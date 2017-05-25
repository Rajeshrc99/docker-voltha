#
# Copyright 2017 the original author or authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Tibit ONU device adapter
"""

import json
import time
import struct
import re

from uuid import uuid4

import arrow
import structlog
from twisted.internet.task import LoopingCall
from zope.interface import implementer

from scapy.layers.inet import ICMP, IP
from scapy.layers.l2 import Ether
from twisted.internet.defer import DeferredQueue, inlineCallbacks
from twisted.internet import reactor

from voltha.core.flow_decomposer import *
from voltha.core.logical_device_agent import mac_str_to_tuple
from common.frameio.frameio import BpfProgramFilter, hexify
from voltha.adapters.interface import IAdapterInterface
from voltha.protos.adapter_pb2 import Adapter, AdapterConfig
from voltha.protos.device_pb2 import Port
from voltha.protos.device_pb2 import DeviceType, DeviceTypes
from voltha.protos.events_pb2 import KpiEventType
from voltha.protos.events_pb2 import MetricValuePairs, KpiEvent
from voltha.protos.health_pb2 import HealthStatus
from voltha.protos.common_pb2 import LogLevel, ConnectStatus
from voltha.protos.common_pb2 import OperStatus, AdminState

from voltha.protos.logical_device_pb2 import LogicalDevice, LogicalPort
from voltha.protos.openflow_13_pb2 import ofp_desc, ofp_port, OFPPF_10GB_FD, \
    OFPPF_FIBER, OFPPS_LIVE, ofp_switch_features, OFPC_PORT_STATS, \
    OFPC_GROUP_STATS, OFPC_TABLE_STATS, OFPC_FLOW_STATS

from scapy.packet import Packet, bind_layers
from scapy.fields import StrField

log = structlog.get_logger()

from voltha.extensions.eoam.EOAM_TLV import AddStaticMacAddress, DeleteStaticMacAddress
from voltha.extensions.eoam.EOAM_TLV import ClearStaticMacTable
from voltha.extensions.eoam.EOAM_TLV import DeviceId
from voltha.extensions.eoam.EOAM_TLV import ClauseSubtypeEnum
from voltha.extensions.eoam.EOAM_TLV import RuleOperatorEnum
from voltha.extensions.eoam.EOAM_TLV import DPoEOpcodeEnum, DPoEVariableResponseCodes
from voltha.extensions.eoam.EOAM_TLV import DPoEOpcode_MulticastRegister, MulticastRegisterSet
from voltha.extensions.eoam.EOAM_TLV import VendorName, OnuMode, HardwareVersion, ManufacturerInfo
from voltha.extensions.eoam.EOAM_TLV import SlowProtocolsSubtypeEnum, DeviceReset
from voltha.extensions.eoam.EOAM_TLV import EndOfPDU

from voltha.extensions.eoam.EOAM import EOAMPayload, EOAMEvent, EOAM_VendSpecificMsg
from voltha.extensions.eoam.EOAM import EOAM_OmciMsg, EOAM_TibitMsg, EOAM_DpoeMsg
from voltha.extensions.eoam.EOAM import EOAMPayload, CableLabs_OUI, Tibit_OUI
from voltha.extensions.eoam.EOAM import DPoEOpcode_GetRequest, DPoEOpcode_SetRequest
from voltha.extensions.eoam.EOAM import mcastIp2McastMac

TIBIT_MSG_WAIT_TIME = 3

### Received OAM Message Types
RxedOamMsgTypeEnum = {
    "Unknown": 0x00,
    # Info PDU - not currently used
    "Info": 0x01,
    # Event Notification - Tibit or DPoE Event
    "Event Notification": 0x02,
    "DPoE Get Response": 0x03,
    "DPoE Set Response": 0x04,
    # Specifically - a File Transfer ACK
    "DPoE File Transfer": 0x05,
    # Contains an embedded OMCI message
    "OMCI Message": 0x06,
    }

Dpoe_Opcodes = {v: k for k, v in DPoEOpcodeEnum.iteritems()}


@implementer(IAdapterInterface)
class TibitOnuAdapter(object):

    name = 'tibit_onu'

    supported_device_types = [
        DeviceType(
            id='tibit_onu',
            adapter=name,
            accepts_bulk_flow_update=True
        )
    ]

    def __init__(self, adapter_agent, config):
        self.adapter_agent = adapter_agent
        self.config = config
        self.descriptor = Adapter(
            id=self.name,
            vendor='Tibit Communications Inc.',
            version='0.1',
            config=AdapterConfig(log_level=LogLevel.INFO)
        )
        self.incoming_messages = DeferredQueue()
        self.mode = "GPON"

    def start(self):
        log.debug('starting')
        log.info('started')

    def stop(self):
        log.debug('stopping')
        log.info('stopped')

    def adapter_descriptor(self):
        return self.descriptor

    def device_types(self):
        return DeviceTypes(items=self.supported_device_types)

    def health(self):
        return HealthStatus(state=HealthStatus.HealthState.HEALTHY)

    def change_master_state(self, master):
        raise NotImplementedError()

    def update_pm_config(self, device, pm_configs):
        raise NotImplementedError()

    def adopt_device(self, device):
        log.info('adopt-device', device=device)
        reactor.callLater(0.1, self._onu_device_activation, device)
        return device

    @inlineCallbacks
    def _onu_device_activation(self, device):
        # first we verify that we got parent reference and proxy info
        assert device.parent_id
        assert device.proxy_address.device_id
        assert device.proxy_address.channel_id

        # Device information will be updated later on
        device.vendor = 'Tibit Communications, Inc.'
        device.model = '10G GPON ONU'
        device.connect_status = ConnectStatus.REACHABLE
        self.adapter_agent.update_device(device)

        # then shortly after we create some ports for the device
        uni_port = Port(
            port_no=2,
            label='UNI facing Ethernet port',
            type=Port.ETHERNET_UNI,
            admin_state=AdminState.ENABLED,
            oper_status=OperStatus.ACTIVE
        )
        self.adapter_agent.add_port(device.id, uni_port)
        self.adapter_agent.add_port(device.id, Port(
            port_no=1,
            label='PON port',
            type=Port.PON_ONU,
            admin_state=AdminState.ENABLED,
            oper_status=OperStatus.ACTIVE,
            peers=[
                Port.PeerPort(
                    device_id=device.parent_id,
                    port_no=device.parent_port_no
                )
            ]
        ))

        # TODO adding vports to the logical device shall be done by agent?
        # then we create the logical device port that corresponds to the UNI
        # port of the device

        # obtain logical device id
        parent_device = self.adapter_agent.get_device(device.parent_id)
        logical_device_id = parent_device.parent_id
        assert logical_device_id

        # we are going to use the proxy_address.channel_id as unique number
        # and name for the virtual ports, as this is guaranteed to be unique
        # in the context of the OLT port, so it is also unique in the context
        # of the logical device
        port_no = device.proxy_address.channel_id
        cap = OFPPF_10GB_FD | OFPPF_FIBER
        self.adapter_agent.add_logical_port(logical_device_id, LogicalPort(
            id=str(port_no),
            ofp_port=ofp_port(
                port_no=port_no,
                hw_addr=mac_str_to_tuple(device.mac_address),
                name='uni-{}'.format(port_no),
                config=0,
                state=OFPPS_LIVE,
                curr=cap,
                advertised=cap,
                peer=cap,
                curr_speed=OFPPF_10GB_FD,
                max_speed=OFPPF_10GB_FD
            ),
            device_id=device.id,
            device_port_no=uni_port.port_no
        ))

        # simulate a proxied message sending and receving a reply
        reply = yield self._message_exchange(device)

        # TODO - Need to add validation of reply and decide what to do upon failure

        # and finally update to "ACTIVE"
        device = self.adapter_agent.get_device(device.id)
        device.oper_status = OperStatus.ACTIVE
        self.adapter_agent.update_device(device)

        # TODO - Disable Stats Reporting for the moment
        #self.start_kpi_collection(device.id)

    def abandon_device(self, device):
        raise NotImplementedError(0
                                  )
    def disable_device(self, device):
        log.info('disable-device', device_id=device.id)
        return device

    def reenable_device(self, device):
        log.info('reenable-device', device_id=device.id)
        return device

    @inlineCallbacks
    def reboot_device(self, device):
        log.info('Rebooting ONU: {}'.format(device.mac_address))

        # Update the operational status to ACTIVATING and connect status to
        # UNREACHABLE
        previous_oper_status = device.oper_status
        previous_conn_status = device.connect_status
        device.oper_status = OperStatus.ACTIVATING
        device.connect_status = ConnectStatus.UNREACHABLE
        self.adapter_agent.update_device(device)

        msg = (
            EOAMPayload() / EOAM_VendSpecificMsg(oui=CableLabs_OUI) /
            EOAM_DpoeMsg(dpoe_opcode = Dpoe_Opcodes["Set Request"], 
                         body=DeviceReset())/
            EndOfPDU()
            )

        action = "Device Reset"

        # send message
        log.info('ONU-send-proxied-message to {} for ONU: {}'.format(action, device.mac_address))
        self.adapter_agent.send_proxied_message(device.proxy_address, msg)

        rc = []
        yield self._handle_set_resp(device, action, rc)

        # Change the operational status back to its previous state.
        device.oper_status = previous_oper_status
        device.connect_status = previous_conn_status
        self.adapter_agent.update_device(device)

        log.info('ONU Rebooted: {}'.format(device.mac_address))

    def delete_device(self, device):
        raise NotImplementedError()

    def get_device_details(self, device):
        raise NotImplementedError()

    @inlineCallbacks
    def update_flows_bulk(self, device, flows, groups):
        log.info('########################################')
        log.info('bulk-flow-update', device_id=device.id,
                 flows=flows, groups=groups)
        assert len(groups.items) == 0, "Cannot yet deal with groups"

        # Clear the existing entries in the Static MAC Address Table
        yield self._send_clear_static_mac_table(device)

        # Re-add the IGMP Multicast Address
        yield self._send_igmp_mcast_addr(device)


        Clause = {v: k for k, v in ClauseSubtypeEnum.iteritems()}
        Operator = {v: k for k, v in RuleOperatorEnum.iteritems()}

        for flow in flows.items:
            in_port = get_in_port(flow)
            assert in_port is not None

            precedence = 255 - min(flow.priority / 256, 255)

            if in_port == 2:
                log.info('#### Upstream Rule ####')

                up_req = (
                    EOAMPayload() / EOAM_VendSpecificMsg(oui=CableLabs_OUI) /
                    EOAM_DpoeMsg(dpoe_opcode=Dpoe_Opcodes["Set Request"])
                    )

                #TODO - There is no body to the message above, is there ever an Upstream Rule

                for field in get_ofb_fields(flow):

                    if field.type == ETH_TYPE:
                        _type = field.eth_type
                        log.info('#### field.type == ETH_TYPE ####',field_type=_type)

                    elif field.type == IP_PROTO:
                        _proto = field.ip_proto
                        log.info('#### field.type == IP_PROTO ####')

                    elif field.type == IN_PORT:
                        _port = field.port
                        log.info('#### field.type == IN_PORT ####', port=_port)

                    elif field.type == VLAN_VID:
                        _vlan_vid = field.vlan_vid & 0xfff
                        log.info('#### field.type == VLAN_VID ####', vlan=_vlan_vid)

                    elif field.type == VLAN_PCP:
                        _vlan_pcp = field.vlan_pcp
                        log.info('#### field.type == VLAN_PCP ####', pcp=_vlan_pcp)

                    elif field.type == UDP_DST:
                        _udp_dst = field.udp_dst
                        log.info('#### field.type == UDP_DST ####')

                    elif field.type == IPV4_DST:
                        _ipv4_dst = field.ipv4_dst
                        log.info('#### field.type == IPV4_DST ####')

                    else:
                        log.info('#### field.type == NOT IMPLEMENTED!! ####')
                        raise NotImplementedError('field.type={}'.format(
                            field.type))

                for action in get_actions(flow):

                    if action.type == OUTPUT:
                        log.info('#### action.type == OUTPUT ####')

                    elif action.type == POP_VLAN:
                        log.info('#### action.type == POP_VLAN ####')

                    elif action.type == PUSH_VLAN:
                        log.info('#### action.type == PUSH_VLAN ####')
                        if action.push.ethertype != 0x8100:
                            log.error('unhandled-tpid',
                                      ethertype=action.push.ethertype)

                    elif action.type == SET_FIELD:
                        log.info('#### action.type == SET_FIELD ####')
                        assert (action.set_field.field.oxm_class ==
                                ofp.OFPXMC_OPENFLOW_BASIC)
                        field = action.set_field.field.ofb_field
                        if field.type == VLAN_VID:
                            pass
                        else:
                            log.error('unsupported-action-set-field-type',
                                      field_type=field.type)
                    else:
                        log.error('UNSUPPORTED-ACTION-TYPE',
                                  action_type=action.type)

            elif in_port == 1:
                log.info('#### Downstream Rule ####')

                #### Loop through fields again...

                for field in get_ofb_fields(flow):

                    if field.type == ETH_TYPE:
                        _type = field.eth_type
                        log.info('#### field.type == ETH_TYPE ####', in_port=in_port,
                                 match=_type)

                    elif field.type == IP_PROTO:
                        _proto = field.ip_proto
                        log.info('#### field.type == IP_PROTO ####', in_port=in_port,
                                 ip_proto=ip_proto)

                    elif field.type == IN_PORT:
                        _port = field.port
                        log.info('#### field.type == IN_PORT ####')

                    elif field.type == VLAN_VID:
                        _vlan_vid = field.vlan_vid & 0xfff
                        log.info('#### field.type == VLAN_VID ####')

                    elif field.type == VLAN_PCP:
                        _vlan_pcp = field.vlan_pcp
                        log.info('#### field.type == VLAN_PCP ####')

                    elif field.type == UDP_DST:
                        _udp_dst = field.udp_dst
                        log.info('#### field.type == UDP_DST ####')

                    elif field.type == IPV4_DST:
                        _ipv4_dst = field.ipv4_dst
                        log.info('#### field.type == IPV4_DST ####')
                        a = int(hex(_ipv4_dst)[2:4], 16)
                        b = int(hex(_ipv4_dst)[4:6], 16)
                        c = int(hex(_ipv4_dst)[6:8], 16)
                        d = int(hex(_ipv4_dst)[8:], 16)
                        dn_req = (
                            EOAMPayload() / EOAM_VendSpecificMsg(oui=CableLabs_OUI) /
                            EOAM_DpoeMsg(dpoe_opcode=Dpoe_Opcodes["Set Request"], body=AddStaticMacAddress(mac=mcastIp2McastMac('%d.%d.%d.%d' % (a,b,c,d)))
                            ))
                        
                        # send message
                        action = "Set Static IP MCAST address"
                        log.info('ONU-send-proxied-message to {} for ONU: {}'.format(action, device.mac_address))
                        self.adapter_agent.send_proxied_message(device.proxy_address,
                                                                dn_req)

                        # Get and process the Set Response
                        rc = []
                        yield self._handle_set_resp(device, action, rc)

                    else:
                        raise NotImplementedError('field.type={}'.format(
                            field.type))

                for action in get_actions(flow):

                    if action.type == OUTPUT:
                        log.info('#### action.type == OUTPUT ####')

                    elif action.type == POP_VLAN:
                        log.info('#### action.type == POP_VLAN ####')

                    elif action.type == PUSH_VLAN:
                        log.info('#### action.type == PUSH_VLAN ####')
                        if action.push.ethertype != 0x8100:
                            log.error('unhandled-ether-type',
                                      ethertype=action.push.ethertype)

                    elif action.type == SET_FIELD:
                        log.info('#### action.type == SET_FIELD ####')
                        assert (action.set_field.field.oxm_class ==
                                ofp.OFPXMC_OPENFLOW_BASIC)
                        field = action.set_field.field.ofb_field
                        if field.type == VLAN_VID:
                            pass
                        else:
                            log.error('unsupported-action-set-field-type',
                                      field_type=field.type)

                    else:
                        log.error('UNSUPPORTED-ACTION-TYPE',
                                  action_type=action.type)

            else:
                raise Exception('Port should be 1 or 2 by our convention')

    def update_flows_incrementally(self, device, flow_changes, group_changes):
        raise NotImplementedError()

    def send_proxied_message(self, proxy_address, msg):
        raise NotImplementedError()

    def receive_proxied_message(self, proxy_address, msg):
        log.info('receive-proxied-message',
                  proxy_address=proxy_address, msg=msg.show(dump=True))
        self.incoming_messages.put(msg)

    @inlineCallbacks
    def _message_exchange(self, device):

        # register for receiving async messages
        self.adapter_agent.register_for_proxied_messages(device.proxy_address)

        # reset incoming message queue
        while self.incoming_messages.pending:
            _ = yield self.incoming_messages.get()

        # send out ping frame to ONU device get device information
        ping_frame = (
            EOAMPayload() / EOAM_VendSpecificMsg(oui=CableLabs_OUI) /
            EOAM_DpoeMsg(dpoe_opcode=Dpoe_Opcodes["Get Request"],
                         body=VendorName() /
                              OnuMode() /
                              HardwareVersion() /
                              ManufacturerInfo()
                              ) /
            EndOfPDU()
            )

        log.info('ONU-send-proxied-message to Get Version Info for ONU: {}'.format(device.mac_address))
        self.adapter_agent.send_proxied_message(device.proxy_address, ping_frame)

        # Loop until we have a Get Response
        ack = False
        while not ack:
            frame = yield self.incoming_messages.get()

            respType = self._get_oam_msg_type(frame)
         
            if (respType == RxedOamMsgTypeEnum["DPoE Get Response"]):
                ack = True
            else:
                # Handle unexpected events/OMCI messages
                self._check_resp(frame)

        if ack:
            log.info('ONU-response received for Get Version Info for ONU: {}'.format(device.mac_address))

            self._process_ping_frame_response(device, frame)


        if self.mode.upper()[0] == "G":  # GPON
            # construct multicast LLID set
            msg = (
                EOAMPayload() / EOAM_VendSpecificMsg(oui=CableLabs_OUI) /
                EOAM_DpoeMsg(dpoe_opcode=Dpoe_Opcodes["Multicast Register"],body=MulticastRegisterSet(MulticastLink=0x10bc, UnicastLink=0)
                ))

            # send message
            log.info('ONU-send-proxied-message to Multicast Register Set for ONU: {}'.format(device.mac_address))
            self.adapter_agent.send_proxied_message(device.proxy_address, msg)

            # The MulticastRegisterSet does not currently return a response. Just hope it worked.

        # by returning we allow the device to be shown as active, which
        # indirectly verified that message passing works

    def receive_packet_out(self, logical_device_id, egress_port_no, msg):
        log.info('packet-out', logical_device_id=logical_device_id,
                 egress_port_no=egress_port_no, msg_len=len(msg))

    def receive_inter_adapter_message(self, msg):
        raise NotImplementedError()

    def suppress_alarm(self, filter):
        raise NotImplementedError()

    def unsuppress_alarm(self, filter):
        raise NotImplementedError()

    def start_kpi_collection(self, device_id):

        """TMP Simulate periodic KPI metric collection from the device"""
        import random

        @inlineCallbacks  # pretend that we need to do async calls
        def _collect(device_id, prefix):

            try:
                # Step 1: gather metrics from device (pretend it here) - examples
                uni_port_metrics = yield dict(
                    tx_pkts=random.randint(0, 100),
                    rx_pkts=random.randint(0, 100),
                    tx_bytes=random.randint(0, 100000),
                    rx_bytes=random.randint(0, 100000),
                )
                pon_port_metrics = yield dict(
                    tx_pkts=uni_port_metrics['rx_pkts'],
                    rx_pkts=uni_port_metrics['tx_pkts'],
                    tx_bytes=uni_port_metrics['rx_bytes'],
                    rx_bytes=uni_port_metrics['tx_bytes'],
                )
                onu_metrics = yield dict(
                    cpu_util=20 + 5 * random.random(),
                    buffer_util=10 + 10 * random.random()
                )

                # Step 2: prepare the KpiEvent for submission
                # we can time-stamp them here (or could use time derived from OLT
                ts = arrow.utcnow().timestamp
                kpi_event = KpiEvent(
                    type=KpiEventType.slice,
                    ts=ts,
                    prefixes={
                        # OLT-level
                        prefix: MetricValuePairs(metrics=onu_metrics),
                        # OLT NNI port
                        prefix + '.nni': MetricValuePairs(metrics=uni_port_metrics),
                        # OLT PON port
                        prefix + '.pon': MetricValuePairs(metrics=pon_port_metrics)
                    }
                )

                # Step 3: submit
                self.adapter_agent.submit_kpis(kpi_event)

            except Exception as e:
                log.exception('failed-to-submit-kpis', e=e)

        prefix = 'voltha.{}.{}'.format(self.name, device_id)
        lc = LoopingCall(_collect, device_id, prefix)
        lc.start(interval=15)  # TODO make this configurable


# Methods for Get / Set  Response Processing from eoam_messages

    def _get_oam_msg_type(self, frame):

        respType = RxedOamMsgTypeEnum["Unknown"]
        recv_frame = frame

        if recv_frame.haslayer(EOAMPayload):
            if recv_frame.haslayer(EOAMEvent):
                recv_frame = RxedOamMsgTypeEnum["Event Notification"]
            elif recv_frame.haslayer(EOAM_OmciMsg):
                respType = RxedOamMsgTypeEnum["OMCI Message"]
            else:
                dpoeOpcode = 0x00
                if recv_frame.haslayer(EOAM_TibitMsg):
                    dpoeOpcode = recv_frame.getlayer(EOAM_TibitMsg).dpoe_opcode;
                elif recv_frame.haslayer(EOAM_DpoeMsg):
                    dpoeOpcode = recv_frame.getlayer(EOAM_DpoeMsg).dpoe_opcode;

                # Get Response
                if (dpoeOpcode == 0x02):
                    respType = RxedOamMsgTypeEnum["DPoE Get Response"]

                # Set Response
                elif (dpoeOpcode == 0x04):
                    respType = RxedOamMsgTypeEnum["DPoE Set Response"]

                # File Transfer ACK
                elif (dpoeOpcode == 0x09):
                    respType = RxedOamMsgTypeEnum["DPoE File Transfer"]
                else:
                    log.info('Unsupported DPoE Opcode {:0>2X}'.format(dpoeOpcode))
        else:
            log.info('Invalid OAM Header')

        return respType


    def _get_value_from_msg(self, frame, branch, leaf):
        retVal = False
        value = 0
        recv_frame = frame

        if recv_frame.haslayer(EOAMPayload):
            payload = recv_frame.payload
            if hasattr(payload, 'body'):
                loadstr = payload.body.load
                # Get a specific TLV value
                (retVal,bytesRead,value,retbranch,retleaf) = self._handle_get_value(loadstr, 0, branch, leaf)
            else:
                log.info('received frame has no payload')
        else:
            log.info('Invalid OAM Header')
        return retVal,value,


    def _handle_get_value(self, loadstr, startOfTlvs, queryBranch, queryLeaf):
        retVal = False;
        value = 0
        branch = 0
        leaf = 0
        bytesRead = startOfTlvs
        loadstrlen    = len(loadstr)

        while (bytesRead <= loadstrlen):
            (branch, leaf) = struct.unpack_from('>BH', loadstr, bytesRead)

            if (branch != 0):
                bytesRead += 3
                length = struct.unpack_from('>B', loadstr, bytesRead)[0]
                bytesRead += 1

                if (length == 1):
                    value = struct.unpack_from(">B", loadstr, bytesRead)[0]
                elif (length == 2):
                    value = struct.unpack_from(">H", loadstr, bytesRead)[0]
                elif (length == 4):
                    value = struct.unpack_from(">I", loadstr, bytesRead)[0]
                elif (length == 8):
                    value = struct.unpack_from(">Q", loadstr, bytesRead)[0]
                else:
                    if (length >= 0x80):
                        log.info('Branch 0x{:0>2X} Leaf 0x{:0>4X} {}'.format(branch, leaf, DPoEVariableResponseCodes[length]))
                        # Set length to zero so bytesRead doesn't get mistakenly incremented below
                        length = 0
                    else:
                        # Attributes with a length of zero are actually 128 bytes long
                        if (length == 0):
                            length = 128;
                        valStr = ">{}s".format(length)
                        value = struct.unpack_from(valStr, loadstr, bytesRead)[0]

                if (length > 0):
                    bytesRead += length

                if (branch != 0xD6):
                    if ( ((queryBranch == 0) and (queryLeaf == 0)) or
                         ((queryBranch == branch) and (queryLeaf == leaf)) ):
                        # Prevent zero-lengthed values from returning success
                        if (length > 0):
                            retVal = True;
                        break
            else:
                break

        if (retVal == False):
            value = 0

        return retVal,bytesRead,value,branch,leaf


    def _check_set_resp(self, frame):
        rc = False
        branch = 0
        leaf = 0
        status = 0
        recv_frame = frame
        if recv_frame.haslayer(EOAMPayload):
            payload = recv_frame.payload
            if hasattr(payload, 'body'):
                loadstr = payload.body.load
                # Get a specific TLV value
                (rc,branch,leaf,status) = self._check_set_resp_attrs(loadstr, 0)
            else:
                log.info('received frame has no payload')
        else:
            log.info('Invalid OAM Header')
        return rc,branch,leaf,status



    def _check_resp(self, frame):
        respType = RxedOamMsgTypeEnum["Unknown"]
        recv_frame = frame
        if recv_frame.haslayer(EOAMPayload):

            if recv_frame.haslayer(EOAMEvent):
                self.handle_oam_event(recv_frame)
            elif recv_frame.haslayer(EOAM_OmciMsg):
                 self.handle_omci(recv_frame)
            else:
                dpoeOpcode = 0x00
                if recv_frame.haslayer(EOAM_TibitMsg):
                    dpoeOpcode = recv_frame.getlayer(EOAM_TibitMsg).dpoe_opcode;
                elif recv_frame.haslayer(EOAM_DpoeMsg):
                    dpoeOpcode = recv_frame.getlayer(EOAM_DpoeMsg).dpoe_opcode;

                if hasattr(recv_frame, 'body'):
                    payload = recv_frame.payload
                    loadstr = payload.body.load

                # Get Response
                if (dpoeOpcode == 0x02):
                    bytesRead = 0
                    rc = True
                    while(rc == True):
                        branch = 0
                        leaf = 0
                        (rc,bytesRead,value,branch,leaf) = self._handle_get_value(loadstr, bytesRead, branch, leaf)
                        if (rc == True):
                            log.info('Branch 0x{:0>2X} Leaf 0x{:0>4X}  value = {}'.format(branch, leaf, value))
                        elif (branch != 0):
                            log.info('Branch 0x{:0>2X} Leaf 0x{:0>4X}  no value'.format(branch, leaf))

                # Set Response
                elif (dpoeOpcode == 0x04):
                    (rc,branch,leaf,status) = self._check_set_resp_attrs(loadstr, 0)
                    if (rc == True):
                        log.info('Set Response had no errors')
                    else:
                        log.info('Branch 0x{:X} Leaf 0x{:0>4X} {}'.format(branch, leaf, DPoEVariableResponseCodes[status]))

                # File Transfer ACK
                elif (dpoeOpcode == 0x09):
                    rc = self._handle_fx_ack(loadstr, bytesRead, block_number)
                else:
                    log.info('Unsupported DPoE Opcode {:0>2X}'.format(dpoeOpcode))
        else:
            log.info('Invalid OAM Header')

        return respType    

    def _check_set_resp_attrs(self, loadstr, startOfTlvs):
        retVal = True;
        branch = 0
        leaf = 0
        length = 0
        bytesRead = startOfTlvs
        loadstrlen    = len(loadstr)

        while (bytesRead <= loadstrlen):
            (branch, leaf) = struct.unpack_from('>BH', loadstr, bytesRead)
#            print "Branch/Leaf        0x{:0>2X}/0x{:0>4X}".format(branch, leaf)

            if (branch != 0):
                bytesRead += 3
                length = struct.unpack_from('>B', loadstr, bytesRead)[0]
#                print "Length:            0x{:0>2X} ({})".format(length,length)
                bytesRead += 1

                if (length >= 0x80):
                    log.info('Branch 0x{:0>2X} Leaf 0x{:0>4X} {}'.format(branch, leaf, DPoEVariableResponseCodes[length]))
                    if (length > 0x80):
                        retVal = False;
                        break;
                else:
                    bytesRead += length

            else:
                break

        return retVal,branch,leaf,length

    def _handle_fx_ack(self, loadstr, startOfXfer, block_number):
        retVal = False
        (fx_opcode, acked_block, response_code) = struct.unpack_from('>BHB', loadstr, startOfXfer)

        #print "fx_opcode:      0x%x" % fx_opcode
        #print "acked_block:    0x%x" % acked_block
        #print "response_code:  0x%x" % response_code


        if (fx_opcode != 0x03):
            log.info('unexpected fx_opcode 0x%x (expected 0x03)' % fx_opcode)
        elif (acked_block != block_number):
            log.info('unexpected acked_block 0x%x (expected 0x%x)' % (acked_block, block_number))
        elif (response_code != 0):
            log.info('unexpected response_code 0x%x (expected 0x00)' % response_code)
        else:
            retVal = True;

    @inlineCallbacks
    def _send_igmp_mcast_addr(self, device):
        # construct install of igmp query address
        msg = (
            EOAMPayload() / EOAM_VendSpecificMsg(oui=CableLabs_OUI) /
            EOAM_DpoeMsg(dpoe_opcode=Dpoe_Opcodes["Set Request"],body=AddStaticMacAddress(mac='01:00:5e:00:00:01')
            ))

        action = "Set Static IGMP MAC address"

        # send message
        log.info('ONU-send-proxied-message to {} for ONU: {}'.format(action, device.mac_address))
        self.adapter_agent.send_proxied_message(device.proxy_address, msg)

        rc = []
        yield self._handle_set_resp(device, action, rc)


    @inlineCallbacks
    def _send_clear_static_mac_table(self, device):
        # construct install of igmp query address
        msg = (
            EOAMPayload() / EOAM_VendSpecificMsg(oui=CableLabs_OUI) /
            EOAM_DpoeMsg(dpoe_opcode=Dpoe_Opcodes["Set Request"],body=ClearStaticMacTable()
            ))

        action = "Clear Static MAC Table"

        # send message
        log.info('ONU-send-proxied-message to {} for ONU: {}'.format(action, device.mac_address))
        self.adapter_agent.send_proxied_message(device.proxy_address, msg)

        rc = []
        yield self._handle_set_resp(device, action, rc)
    

    @inlineCallbacks
    def _handle_set_resp(self, device, action, retcode):
        # Get and process the Set Response
        ack = False
        start_time = time.time()

        # Loop until we have a set response or timeout
        while not ack:
            frame = yield self.incoming_messages.get()
            #TODO - Need to add propoer timeout functionality
            #if (time.time() - start_time) > TIBIT_MSG_WAIT_TIME or (frame is None):
            #    break  # don't wait forever

            respType = self._get_oam_msg_type(frame)
            log.info('Received OAM Message 0x %s' % str(respType))

            #Check that the message received is a Set Response
            if (respType == RxedOamMsgTypeEnum["DPoE Set Response"]):
                ack = True
            else:
                # Handle unexpected events/OMCI messages
                self._check_resp(frame)

        # Verify Set Response
        rc = False
        if ack:
            (rc,branch,leaf,status) = self._check_set_resp(frame)
            if (rc is False):
                log.info('Set Response had errors - Branch 0x{:X} Leaf 0x{:0>4X} {}'.format(branch, leaf, DPoEVariableResponseCodes[status]))
        
        if (rc is True):
            log.info('ONU-response received for {} for ONU: {}'.format(action, device.mac_address))
        else:
            log.info('BAD ONU-response received for {} for ONU: {}'.format(action, device.mac_address))

        retcode.append(rc)

    def _process_ping_frame_response(self, device, frame):

        vendor = [0xD7, 0x0011]
        ponMode = [0xB7, 0x0105]
        hw_version = [0xD7, 0x0013]
        manufacturer =  [0xD7, 0x0006]
        branch_leaf_pairs = [vendor, ponMode, hw_version, manufacturer]
                    
        for pair in branch_leaf_pairs:
            temp_pair = pair
            (rc, value) = (self._get_value_from_msg(frame, pair[0], pair[1]))
            temp_pair.append(rc)
            temp_pair.append(value)
            if rc:
                overall_rc = True
            else: 
                log.info('Failed to get valid response for Branch 0x{:X} Leaf 0x{:0>4X} '.format(temp_pair[0], temp_pair[1]))
                ack = True

        if vendor[rc]:
            device.vendor = vendor.pop()
            if device.vendor.endswith(''):
                device.vendor = device.vendor[:-1]
        else:
            device.vendor = "UNKNOWN"
            
        # mode: 3 = EPON OLT, 7 = GPON OLT
        # mode: 2 = EPON ONU, 6 = GPON ONU    
        if ponMode[rc]:
            value = ponMode.pop()
            mode = "UNKNOWN"
            self.mode = "UNKNOWN"

            if value == 6:
                mode = "10G GPON ONU"
                self.mode = "GPON"
            if value == 2:
                mode = "10G EPON ONU"
                self.mode = "EPON"
            if value == 1:
                mode = "10G Point to Point"
                self.mode = "Unsupported"

            device.model = mode

        else:
            device.model = "UNKNOWN"
            self.mode = "UNKNOWN"

        log.info("PON Mode is {}".format(self.mode))
                
        if hw_version[rc]:
            device.hardware_version = hw_version.pop()
            if device.hardware_version.endswith(''):
                device.hardware_version = device.hardware_version[:-1]
        else:
            device.hardware_version = "UNKNOWN"

        if manufacturer[rc]:
            manu_value = manufacturer.pop()
            device.firmware_version = re.search('\Firmware: (.+?) ', manu_value).group(1)
            device.software_version = re.search('\Build: (.+?) ', manu_value).group(1)
            device.serial_number = re.search('\Serial #: (.+?) ', manu_value).group(1)
        else:
            device.firmware_version = "UNKNOWN"
            device.software_version = "UNKNOWN"
            device.serial_number = "UNKNOWN"

        device.connect_status = ConnectStatus.REACHABLE
