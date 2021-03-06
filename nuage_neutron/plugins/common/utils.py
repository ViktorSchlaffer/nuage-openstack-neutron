# Copyright 2015 Alcatel-Lucent USA Inc.
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

import contextlib
import functools
import inspect
import sys
import time

import netaddr
import six

from neutron_lib import constants as lib_constants
from oslo_log import log as logging

from nuage_neutron.plugins.common import exceptions as nuage_exc
from nuage_neutron.vsdclient.common import constants as vsd_constants
from nuage_neutron.vsdclient.restproxy import RESTProxyError


class SubnetUtilsBase(object):

    @staticmethod
    def _is_v4_ip(ip):
        return (netaddr.IPAddress(ip['ip_address']).version ==
                lib_constants.IP_VERSION_4)

    @staticmethod
    def _is_v6_ip(ip):
        return (netaddr.IPAddress(ip['ip_address']).version ==
                lib_constants.IP_VERSION_6)

    @staticmethod
    def _is_ipv4(subnet):
        return subnet['ip_version'] == lib_constants.IP_VERSION_4

    @staticmethod
    def _is_ipv6(subnet):
        return subnet['ip_version'] == lib_constants.IP_VERSION_6

    @staticmethod
    def _is_equal_ip(ip1, ip2):
        return SubnetUtilsBase.compare_ip(ip1, ip2)

    @staticmethod
    def _is_l2(nuage_subnet_mapping):
        """_is_l2 : indicated whether the subnet ~ this mapping is a l2 subnet

        :type nuage_subnet_mapping: dict
        """
        return bool(nuage_subnet_mapping['nuage_l2dom_tmplt_id'])

    @staticmethod
    def _is_l3(nuage_subnet_mapping):
        return not SubnetUtilsBase._is_l2(nuage_subnet_mapping)

    @staticmethod
    def _is_vsd_mgd(nuage_subnet_mapping):
        """_is_vsd_mgd : indicated whether the subnet ~ this mapping is vsd mgd

        :type nuage_subnet_mapping: dict
        """
        return bool(nuage_subnet_mapping['nuage_managed_subnet'])

    @staticmethod
    def _is_os_mgd(nuage_subnet_mapping):
        return not SubnetUtilsBase._is_vsd_mgd(nuage_subnet_mapping)

    @staticmethod
    def compare_ip(ip1, ip2):
        return ((ip1 is None and ip2 is None) or
                (ip1 is not None and ip2 is not None and
                 netaddr.IPAddress(ip1) == netaddr.IPAddress(ip2)))

    @staticmethod
    def compare_cidr(cidr1, cidr2):
        return ((cidr1 is None and cidr2 is None) or
                (cidr1 is not None and cidr2 is not None and
                 SubnetUtilsBase.normalize_cidr(cidr1) ==
                 SubnetUtilsBase.normalize_cidr(cidr2)))

    @staticmethod
    def normalize_cidr(value):
        try:
            ip = netaddr.IPNetwork(value).cidr
            return six.text_type(ip)
        except netaddr.core.AddrFormatError:
            pass
        return value

    @staticmethod
    def _get_domain_type_id_from_vsd_subnet(vsdclient, vsd_subnet):
        if vsd_subnet['type'] == vsd_constants.L2DOMAIN:
            domain_id = vsd_subnet['ID']
            domain_type = vsd_constants.L2DOMAIN
        else:
            # find l3 domain
            domain_id = vsdclient.get_l3domain_id_by_domain_subnet_id(
                vsd_subnet['ID'])
            domain_type = vsd_constants.DOMAIN
        return domain_type, domain_id


def get_logger(name=None, fn=None):
    return logging.getLogger(fn.__module__ if fn else name)


def context_log(fn):
    def wrapped(*args, **kwargs):
        instance = args[0]
        class_name = instance.__class__.__name__
        method_name = fn.__name__
        context = args[1]
        log = get_logger(fn=fn)
        log.debug('%s method %s is getting called with context.current %s, '
                  'context.original %s',
                  class_name, method_name, context.current, context.original)
        return fn(*args, **kwargs)
    return wrapped


class Ignored(object):
    """Class that will evaluate to False in if-statement and contains error.

    This is returned when exceptions are silently ignored from vsdclient.
    It will return false when doing if x:
    But it's still possible to raise the original exception by doing
    raise x.exception
    """

    def __init__(self, exception):
        self.exception = exception

    def __bool__(self):
        return False


def retry_on_vsdclient_error(fn, nr_attempts=5, vsd_error_codes=None):
    """Retry function on vsdclient error

    :param fn: function to (re)try
    :param nr_attempts
    :param vsd_error_codes: vsd_error_codes to retry [(http_code, vsd_code)]
        [(409,'7010')]
    """
    def wrapped(*args, **kwargs):
        attempt = 1
        log = None
        fn_name = None
        while attempt <= nr_attempts:
            try:
                return fn(*args, **kwargs)
            except RESTProxyError as e:
                if not log:
                    log = get_logger(fn=fn)
                    # when function fn is wrapped, all you see is 'wrapped'
                    # adding info about the caller method is useful therefore
                    caller_name = inspect.stack()[1][3]
                    fn_name = fn.__name__ + '[' + caller_name + ']'
                if attempt == nr_attempts:
                    log.debug('Failed to execute {} {} times.'.format(
                        fn_name, nr_attempts)
                    )
                    raise
                if (e.code, e.vsd_code) in vsd_error_codes:
                    log.debug('Attempt {} of {} to execute {} failed.'.format(
                        attempt, nr_attempts, fn_name)
                    )
                    attempt += 1
                    time.sleep(0.2)
                else:
                    log.debug('Non retry-able error '
                              'encountered on {}.'.format(fn_name))
                    raise
    return wrapped


def handle_nuage_api_errorcode(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)

        except RESTProxyError as e:
            _, _, tb = sys.exc_info()
            if e.code:  # there is a clear error code -> Bad request (400)
                # Reason we overwrite any VSD http error code to 400 is
                # that we don't want to reflect VSD behavior directly in
                # Neutron. E.g. 404 in Neutron refers to Neutron entity
                # not found ; we don't want VSD entity not found to be
                # reported same way. At least, that decision was made in
                # early days of Nuage.
                six.reraise(nuage_exc.NuageBadRequest,
                            nuage_exc.NuageBadRequest(msg=str(e)), tb)
            else:  # there is not --> Nuage API error (leading to 500)
                six.reraise(nuage_exc.NuageAPIException,
                            nuage_exc.NuageAPIException(msg=str(e)), tb)
    return wrapped


def ignore_no_update(fn):
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RESTProxyError as e:
            # This should never go to the user. Neutron does not complain
            # when updating to the same values.
            if str(e.vsd_code) == '2039':  # "There are no attribute changes
                #                             to modify the entity."
                return Ignored(e)
            raise
    return wrapped


def ignore_not_found(fn):
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RESTProxyError as e:
            # We probably want to ignore 404 errors when we're deleting anyway.
            if str(e.vsd_code) == '404':
                return Ignored(e)
            raise
    return wrapped


def filters_to_vsd_filters(filterables, filters, os_to_vsd):
    """Translates openstack filters to vsd filters.

    :param filterables: The attributes which are filterable on VSD.
    :param filters: the neutron filters list from a list request.
    :param os_to_vsd: a dict where the key is the neutron name, and the key is
     the vsd attribute name. For example {'rd': 'routeDistinguisher', ...}
     the key can also be a method which will be called with this method's
     return dict and the 'filters' parameter.
    :return: A dict with vsd-friendly keys and values taken from the filters
     parameter
    """

    if not filters or not filterables or not os_to_vsd:
        return {}
    vsd_filters = {}
    for filter in filterables:
        if filter in filters:
            vsd_key = os_to_vsd[filter]
            if hasattr(vsd_key, '__call__'):
                vsd_key(vsd_filters, filters)
            else:
                vsd_filters[vsd_key] = filters[filter][0]
    return vsd_filters


def add_rollback(rollbacks, method, *args, **kwargs):
    rollbacks.append(functools.partial(method, *args, **kwargs))


@contextlib.contextmanager
def rollback():
    rollbacks = []
    log = get_logger()
    try:
        yield functools.partial(add_rollback, rollbacks)
    except Exception:
        for action in reversed(rollbacks):
            try:
                action()
            except Exception:
                log.exception("Rollback failed.")
        raise


def collect_all_remote_security_groups(core_plugin, context, top_level_sg_id,
                                       analysed_securitygroup_ids):
    # When referencing a remote_group_id we create the corresponding
    # Policygroup. When later on creating a PG we will not consider the
    # remote PG anymore for rule creation so we have to do that now

    # SG objects to create
    collected_securitygroups = []
    # Running queue of SG to be investigated for remote group id rules
    securitygroups_to_analyse = {top_level_sg_id}
    while len(securitygroups_to_analyse) != 0:
        sg_id = securitygroups_to_analyse.pop()
        if securitygroups_to_analyse in analysed_securitygroup_ids:
            continue
        analysed_securitygroup_ids.add(sg_id)
        sg = core_plugin.get_security_group(context, sg_id)
        for sg_rule in sg['security_group_rules']:
            remote_id = sg_rule.get('remote_group_id')
            if remote_id and remote_id not in analysed_securitygroup_ids:
                securitygroups_to_analyse.add(remote_id)
        collected_securitygroups.append(sg)
    return collected_securitygroups
