# Copyright (c) 2011 OpenStack, LLC.
# All Rights Reserved.
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

"""The hosts admin extension."""

import webob.exc
from xml.dom import minidom
from xml.parsers import expat

from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.api.openstack import xmlutil
from nova import compute
from nova import exception
from nova.openstack.common import log as logging

LOG = logging.getLogger(__name__)
authorize = extensions.extension_authorizer('compute', 'hosts')


class HostIndexTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('hosts')
        elem = xmlutil.SubTemplateElement(root, 'host', selector='hosts')
        elem.set('host_name')
        elem.set('service')

        return xmlutil.MasterTemplate(root, 1)


class HostUpdateTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('host')
        root.set('host')
        root.set('status')
        root.set('maintenance_mode')

        return xmlutil.MasterTemplate(root, 1)


class HostActionTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('host')
        root.set('host')
        root.set('power_action')

        return xmlutil.MasterTemplate(root, 1)


class HostShowTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('host')
        elem = xmlutil.make_flat_dict('resource', selector='host',
                                      subselector='resource')
        root.append(elem)

        return xmlutil.MasterTemplate(root, 1)


class HostUpdateDeserializer(wsgi.XMLDeserializer):
    def default(self, string):
        try:
            node = minidom.parseString(string)
        except expat.ExpatError:
            msg = _("cannot understand XML")
            raise exception.MalformedRequestBody(reason=msg)

        updates = {}
        updates_node = self.find_first_child_named(node, 'updates')
        if updates_node is not None:
            maintenance = self.find_first_child_named(updates_node,
                                                      'maintenance_mode')
            if maintenance is not None:
                updates[maintenance.tagName] = self.extract_text(maintenance)

            status = self.find_first_child_named(updates_node, 'status')
            if status is not None:
                updates[status.tagName] = self.extract_text(status)

        return dict(body=updates)


class HostController(object):
    """The Hosts API controller for the OpenStack API."""
    def __init__(self):
        self.api = compute.HostAPI()
        super(HostController, self).__init__()

    @wsgi.serializers(xml=HostIndexTemplate)
    def index(self, req):
        """
        :returns: A dict in the format:

            {'hosts': [{'host_name': 'some.host.name',
               'service': 'cells'},
              {'host_name': 'some.other.host.name',
               'service': 'cells'},
              {'host_name': 'some.celly.host.name',
               'service': 'cells'},
              {'host_name': 'console1.host.com',
               'service': 'consoleauth'},
              {'host_name': 'network1.host.com',
               'service': 'network'},
              {'host_name': 'netwwork2.host.com',
               'service': 'network'},
              {'host_name': 'sched1.host.com',
               'service': 'scheduler'},
              {'host_name': 'sched2.host.com',
               'service': 'scheduler'},
              {'host_name': 'vol1.host.com',
               'service': 'volume'}]}
        """
        context = req.environ['nova.context']
        authorize(context)
        zone = req.GET.get('zone', None)
        data = self.api.list_hosts(context, zone)

        return {'hosts': data}

    @wsgi.serializers(xml=HostUpdateTemplate)
    @wsgi.deserializers(xml=HostUpdateDeserializer)
    def update(self, req, id, body):
        """
        :param body: example format {'status': 'enable',
                                     'maintenance_mode': 'enable'}
        :returns:
        """
        def read_enabled(orig_val, msg):
            """
            :param orig_val: A string with either 'enable' or 'disable'. May
                             be surrounded by whitespace, and case doesn't
                             matter
            :param msg: The message to be passed to HTTPBadRequest. A single
                        %s will be replaced with orig_val.
            :returns:   True for 'enabled' and False for 'disabled'
            """
            val = orig_val.strip().lower()
            if val == "enable":
                return True
            elif val == "disable":
                return False
            else:
                raise webob.exc.HTTPBadRequest(explanation=msg % orig_val)
        context = req.environ['nova.context']
        authorize(context)
        # See what the user wants to 'update'
        params = dict([(k.strip().lower(), v) for k, v in body.iteritems()])
        orig_status = status = params.pop('status', None)
        orig_maint_mode = maint_mode = params.pop('maintenance_mode', None)
        # Validate the request
        if len(params) > 0:
            # Some extra param was passed. Fail.
            explanation = _("Invalid update setting: '%s'") % params.keys()[0]
            raise webob.exc.HTTPBadRequest(explanation=explanation)
        if orig_status is not None:
            status = read_enabled(orig_status, _("Invalid status: '%s'"))
        if orig_maint_mode is not None:
            maint_mode = read_enabled(orig_maint_mode, _("Invalid mode: '%s'"))
        if status is None and maint_mode is None:
            explanation = _("'status' or 'maintenance_mode' needed for "
                            "host update")
            raise webob.exc.HTTPBadRequest(explanation=explanation)
        # Make the calls and merge the results
        result = {'host': id}
        if status is not None:
            result['status'] = self._set_enabled_status(context, id, status)
        if maint_mode is not None:
            result['maintenance_mode'] = self._set_host_maintenance(context,
                id, maint_mode)
        return result

    def _set_host_maintenance(self, context, host_name, mode=True):
        """Start/Stop host maintenance window. On start, it triggers
        guest VMs evacuation."""
        LOG.audit(_("Putting host %(host_name)s in maintenance "
                    "mode %(mode)s.") % locals())
        try:
            result = self.api.set_host_maintenance(context, host_name, mode)
        except NotImplementedError:
            msg = _("Virt driver does not implement host maintenance mode.")
            raise webob.exc.HTTPNotImplemented(explanation=msg)
        except exception.NotFound as e:
            raise webob.exc.HTTPNotFound(explanation=e.message)
        if result not in ("on_maintenance", "off_maintenance"):
            raise webob.exc.HTTPBadRequest(explanation=result)
        return result

    def _set_enabled_status(self, context, host_name, enabled):
        """Sets the specified host's ability to accept new instances.
        :param enabled: a boolean - if False no new VMs will be able to start
        on the host"""
        if enabled:
            LOG.audit(_("Enabling host %s.") % host_name)
        else:
            LOG.audit(_("Disabling host %s.") % host_name)
        try:
            result = self.api.set_host_enabled(context, host_name=host_name,
                    enabled=enabled)
        except NotImplementedError:
            msg = _("Virt driver does not implement host disabled status.")
            raise webob.exc.HTTPNotImplemented(explanation=msg)
        except exception.NotFound as e:
            raise webob.exc.HTTPNotFound(explanation=e.message)
        if result not in ("enabled", "disabled"):
            raise webob.exc.HTTPBadRequest(explanation=result)
        return result

    def _host_power_action(self, req, host_name, action):
        """Reboots, shuts down or powers up the host."""
        context = req.environ['nova.context']
        authorize(context)
        try:
            result = self.api.host_power_action(context, host_name=host_name,
                    action=action)
        except NotImplementedError:
            msg = _("Virt driver does not implement host power management.")
            raise webob.exc.HTTPNotImplemented(explanation=msg)
        except exception.NotFound as e:
            raise webob.exc.HTTPNotFound(explanation=e.message)
        return {"host": host_name, "power_action": result}

    @wsgi.serializers(xml=HostActionTemplate)
    def startup(self, req, id):
        return self._host_power_action(req, host_name=id, action="startup")

    @wsgi.serializers(xml=HostActionTemplate)
    def shutdown(self, req, id):
        return self._host_power_action(req, host_name=id, action="shutdown")

    @wsgi.serializers(xml=HostActionTemplate)
    def reboot(self, req, id):
        return self._host_power_action(req, host_name=id, action="reboot")

    @wsgi.serializers(xml=HostShowTemplate)
    def show(self, req, id):
        """Shows the physical/usage resource given by hosts.

        :param id: hostname
        :returns: expected to use HostShowTemplate.
            ex.::

                {'host': {'resource':D},..}
                D: {'host': 'hostname','project': 'admin',
                    'cpu': 1, 'memory_mb': 2048, 'disk_gb': 30}
        """
        context = req.environ['nova.context']
        try:
            data = self.api.describe_host(context, id)
        except exception.NotFound as e:
            raise webob.exc.HTTPNotFound(explanation=e.message)
        except exception.AdminRequired:
            msg = _("Describe-resource is admin only functionality")
            raise webob.exc.HTTPForbidden(explanation=msg)
        return {'host': data}


class Hosts(extensions.ExtensionDescriptor):
    """Admin-only host administration."""

    name = "Hosts"
    alias = "os-hosts"
    namespace = "http://docs.openstack.org/compute/ext/hosts/api/v1.1"
    updated = "2011-06-29T00:00:00+00:00"

    def get_resources(self):
        resources = [extensions.ResourceExtension('os-hosts',
                HostController(),
                collection_actions={'update': 'PUT'},
                member_actions={"startup": "GET", "shutdown": "GET",
                        "reboot": "GET"})]
        return resources
