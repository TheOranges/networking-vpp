# Copyright (c) 2016 Cisco Systems, Inc.
# All Rights Reserved
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


# This is a simple Flask application that provides REST APIs by which
# compute and network services can communicate, plus a REST API for
# debugging using a CLI client.

# Note that it does *NOT* at this point have a persistent database, so
# restarting this process will make Gluon forget about every port it's
# learned, which will not do your system much good (the data is in the
# global 'backends' and 'ports' objects).  This is for simplicity of
# demonstration; we have a second codebase already defined that is
# written to OpenStack endpoint principles and includes its ORM, so
# that work was not repeated here where the aim was to get the APIs
# worked out.  The two codebases will merge in the future.

from flask import Flask
from flask_restful import Api
from flask_restful import reqparse
from flask_restful import Resource
import logging
import logging.handlers
import os
import sys
import vpp
from neutron.agent.linux import bridge_lib
from neutron.common import constants as n_const


# Basic Flask RESTful app setup
app = Flask('vpp-agent')

######################################################################

# This mirrors functionality in Neutron so that we're creating a name
# that Neutron can find for its agents.

DEV_NAME_PREFIX = n_const.TAP_DEVICE_PREFIX
def get_tap_name(uuid):
    return n_const.TAP_DEVICE_PREFIX + uuid[0:11]

# This mirrors functionality in Nova so that we're creating a vhostuser
# name that it will be able to locate

VHOSTUSER_DIR = '/tmp'
def get_vhostuser_name(uuid):
    return os.path.join(VHOSTUSER_DIR, uuid)

######################################################################



class VPPForwarder(object):

    def __init__(self, external_if):
        self.vpp = vpp.VPPInterface()
        self.external_if = external_if

        self.networks = {}      # vlan: bridge index
        self.interfaces = {}    # uuid: if idx

        for (ifname, f) in self.vpp.get_interfaces():
            # Clean up interfaces from previous runs

            # TODO(ijw) can't easily SPOT VLAN subifs to delete

            if ifname.startswith('tap-'):
                # all VPP tap interfaces are of this form
                self.vpp.delete_tap(f.swifindex)
            elif ifname.startswith('VirtualEthernet'):
                # all VPP vhostuser interfaces are of this form
                self.vpp.delete_vhostuser(f.swifindex)

            ext_ifstruct = self.vpp.get_interface(external_if)
            self.ext_ifidx = ext_ifstruct.swifindex

    # This, here, is us creating a VLAN backed network
    def network_on_host(self, vlan):
        if vlan not in self.networks:
            # TODO(ijw): bridge domains have no distinguishing marks.
            # VPP needs to allow us to name or label them so that we
            # can find them when we restart

            # TODO(ijw): this VLAN subinterface may already exist, and
            # may even be in another bridge domain already (see
            # above).
            if_upstream = self.vpp.create_vlan_subif(self.ext_ifidx, vlan)
            self.vpp.ifup(if_upstream)

            self.vpp.create_bridge_domain(vlan)
	    br = vlan  # May not remain this way but we use the VLAN ID as the bridge ID

            self.vpp.add_to_bridge(br, if_upstream)

            self.networks[vlan] = br

        return self.networks[vlan]

    ########################################
    # stolen from LB driver
    def _bridge_exists_and_ensure_up(self, bridge_name):
        """Check if the bridge exists and make sure it is up."""
        br = ip_lib.IPDevice(bridge_name)
        br.set_log_fail_as_error(False)
        try:
            # If the device doesn't exist this will throw a RuntimeError
            br.link.set_up()
        except RuntimeError:
            return False
        return True

    def ensure_bridge(self, bridge_name):
        """Create a bridge unless it already exists."""
        # _bridge_exists_and_ensure_up instead of device_exists is used here
        # because there are cases where the bridge exists but it's not UP,
        # for example:
        # 1) A greenthread was executing this function and had not yet executed
        # "ip link set bridge_name up" before eventlet switched to this
        # thread running the same function
        # 2) The Nova VIF driver was running concurrently and had just created
        #    the bridge, but had not yet put it UP
        if not self._bridge_exists_and_ensure_up(bridge_name):
            bridge_device = bridge_lib.BridgeDevice.addbr(bridge_name)
            if bridge_device.setfd(0):
                return
            if bridge_device.disable_stp():
                return
            if bridge_device.disable_ipv6():
                return
            if bridge_device.link.set_up():
                return
        else:
            bridge_device = bridge_lib.BridgeDevice(bridge_name)

        if not interface:
            return bridge_name

    # end theft
    ########################################

    def create_interface_on_host(self, type, uuid, mac):
        if uuid not in self.interfaces:
            app.logger.debug('bind port - not in list %s', ', '.join(self.interfaces.keys()))

            if type == 'maketap' or type == 'plugtap':
                # TODO(ijw): naming not obviously consistent with Neutron's naming
                name = uuid[0:11]
                iface = self.vpp.create_tap('vht-' + name, mac)
                if type == 'maketap':
                    props = {'vif_type': 'maketap', 'name': 'vht-' + name}
                else:
                    br = ensure_bridge('br-' + name)
                    props = {'vif_type': 'plugtap', 'name': 'br-' + name}

                    # TODO(ijw): someone somewhere ought to be sorting the MTUs out
                    bridge_lib.BridgeDevice(br).addif('tap-' + name)
                                        
            elif type == 'vhostuser':
                path = get_vhostuser_name(uuid)
                iface = self.vpp.create_vhostuser(path, mac)
                props = {'vif_type': 'vhostuser', 'path': uuid}
            else:
                raise Exception('unsupported interface type')

            self.interfaces[uuid] = (iface, props)
        else:
            app.logger.debug('skipping a repeated bind')
        return self.interfaces[uuid]

    def bind_interface_on_host(self, type, uuid, mac, vlan):
        net_br_idx = self.network_on_host(vlan)

        (iface, props) = self.create_interface_on_host(type, uuid, mac)

        self.vpp.ifup(iface)
        self.vpp.add_to_bridge(net_br_idx, iface)

        return props

    def unbind_interface_on_host(self, type, uuid):
        app.logger.debug('TODO(ijw) unbind port %s', uuid)
        
        pass


######################################################################


vppf = VPPForwarder('GigabitEthernet2/2/0')  # TODO(ijw) make config


class PortBind(Resource):
    bind_args = reqparse.RequestParser()
    bind_args.add_argument('mac_address', type=str, required=True)
    bind_args.add_argument('mtu', type=str, required=True)
    bind_args.add_argument('segmentation_id', type=int, required=True)
    bind_args.add_argument('network_type', type=str, required=True)
    bind_args.add_argument('host', type=str, required=True)

    def put(self, id):
        global vppf

        args = self.bind_args.parse_args()
	app.logger.debug('on host %s, binding %s %d to mac %s id %s' % (args['host'], 
                                                                        args['network_type'], 
                                                                        args['segmentation_id'], 
                                                                        args['mac_address'], id))
        vppf.bind_interface_on_host('vhostuser',
                                    id,
                                    args['mac_address'],
                                    args['segmentation_id'])


class PortUnbind(Resource):

    def __init(self, *args, **kwargs):
        super('PortBind', self).__init__(*args, **kwargs)

    def put(self, id, host):
        global vppf

        vppf.unbind_interface_on_host(id)


api = Api(app)

api.add_resource(PortBind, '/ports/<id>/bind')
api.add_resource(PortUnbind, '/ports/<id>/unbind/<host>')


def main():
    app.debug = True

    logger = logging.getLogger('werkzeug')
    logger.setLevel(logging.INFO)

    # Basic log config
    app.logger.debug('Debug logging enabled')
    # TODO(ijw) port etc. should probably be configurable.
    app.run(port=2704)

if __name__ == '__main__':
    main()
