# -*- coding: utf-8 -*-
"""
2016-02-19 Cornelius Kölbel <cornelius.koelbel@netknights.it>
           Add the possibility to check objectGUID
2015-01-31 Change responses.py to be able to run with SMTP
        Cornelius Kölbel <cornelius@privacyidea.org>

Original responses.py is:
Copyright 2013 Dropbox, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from __future__ import (
    absolute_import, print_function, division, unicode_literals
)

import six
import ldap3
from ldap3.utils.conv import check_escape

try:
    from six import cStringIO as BufferIO
except ImportError:
    from six import StringIO as BufferIO

import inspect
from collections import namedtuple, Sequence, Sized
from functools import update_wrapper

Call = namedtuple('Call', ['request', 'response'])

_wrapper_template = """\
def wrapper%(signature)s:
    with ldap3mock:
        return func%(funcargs)s
"""


def get_wrapped(func, wrapper_template, evaldict):
    # Preserve the argspec for the wrapped function so that testing
    # tools such as pytest can continue to use their fixture injection.
    args, a, kw, defaults = inspect.getargspec(func)
    values = args[-len(defaults):] if defaults else None

    signature = inspect.formatargspec(args, a, kw, defaults)
    is_bound_method = hasattr(func, '__self__')
    if is_bound_method:
        args = args[1:]     # Omit 'self'
    callargs = inspect.formatargspec(args, a, kw, values,
                                     formatvalue=lambda v: '=' + v)

    ctx = {'signature': signature, 'funcargs': callargs}
    six.exec_(wrapper_template % ctx, evaldict)

    wrapper = evaldict['wrapper']

    update_wrapper(wrapper, func)
    if is_bound_method:
        wrapper = wrapper.__get__(func.__self__, type(func.__self__))
    return wrapper


class CallList(Sequence, Sized):
    def __init__(self):
        self._calls = []

    def __iter__(self):
        return iter(self._calls)

    def __len__(self):
        return len(self._calls)

    def __getitem__(self, idx):
        return self._calls[idx]

    def setdata(self, request, response):
        self._calls.append(Call(request, response))

    def reset(self):
        self._calls = []


class Connection(object):

    class Extend(object):

        class Standard(object):

            def __init__(self, connection):
                self.connection = connection

            def paged_search(self, **kwargs):
                self.connection.search(search_base=kwargs.get("search_base"),
                                       search_scope=kwargs.get("search_scope"),
                                       search_filter=kwargs.get(
                                           "search_filter"),
                                       attributes=kwargs.get("attributes"),
                                       paged_size=kwargs.get("page_size"),
                                       size_limit=kwargs.get("size_limit"),
                                       paged_cookie=None)
                return self.connection.response

        def __init__(self, connection):
            self.standard = self.Standard(connection)

    def __init__(self, directory=None):
        if directory is None:
                directory = []
        import copy
        self.directory = copy.deepcopy(directory)
        self.bound = False
        self.extend = self.Extend(self)

    def set_directory(self, directory):
        self.directory = directory

    @staticmethod
    def open():
        return

    def bind(self):
        return self.bound

    def search(self, search_base=None, search_scope=None,
               search_filter=None, attributes=None, paged_size=5,
               size_limit=0, paged_cookie=None):
        self.response = []
        self.result = {}
        condition = {}
        # (&(cn=*)(cn=bob)) -> (cn=*)(cn=bob)
        search_filter = search_filter[2:-1]
        while search_filter:
            pos = search_filter.find(')')+1
            cur = search_filter[0:pos]
            cur = cur[1:-1]
            cur = cur.strip("|").strip("(").strip(")")
            if cur:
                (k, v) = cur.split("=")
                if v != "*":
                    condition[k] = check_escape(v)
            search_filter = search_filter[pos:]
        for entry in self.directory:
            dn = entry.get("dn")
            if dn.endswith(search_base):
                # The entry is in the correct search base
                # NOTE: Checking condition works only for one condition
                found = True
                for k, v in condition.iteritems():
                    try:
                        lesser = False
                        unequal = False
                        if k.endswith("<"):
                            lesser = True
                            k = k.strip("<")
                        if k.endswith("!"):
                            unequal = True
                            k = k.strip("!")
                        if k in entry.get("attributes").keys():
                            if unequal:
                                ldap_value = entry.get("attributes").get(k)
                                requested_value = int(v)
                                found = found and (ldap_value !=
                                                   requested_value)
                            elif lesser:
                                # first we try <=
                                ldap_value = entry.get("attributes").get(k)
                                requested_value = int(v)
                                # If the LDAP value is greater, then we do not
                                # return this entry
                                found = found and (ldap_value < requested_value)
                            elif entry.get("attributes").get(k) == v:
                                # exact matching
                                found = found and True
                            elif "*" in v:
                                # rough substring matching
                                # We assume, that there are only leading and
                                # trailing asterisks
                                v = v.replace("*", "")
                                if v not in entry.get("attributes").get(k, ""):
                                    found = False
                            else:
                                found = found and False
                        else:
                            # The entry does not have such an attribute at all!
                            found = False
                    except UnicodeDecodeError:
                        # This happens when we check for a "*" in the binary
                        # string as it occurs in objectGUID
                        print("OK, some potential objectGUID exception. But "
                              "this is OK")
                        found = False
                if found:
                    entry["type"] = "searchResEntry"
                    self.response.append(entry)

        return True

    def unbind(self):
        return True


class Ldap3Mock(object):

    def __init__(self):
        self._calls = CallList()
        self.directory = []
        self.reset()

    def reset(self):
        self._calls.reset()

    def setLDAPDirectory(self, directory=None):
        if directory is None:
                directory = []
        self.directory = directory

    @property
    def calls(self):
        return self._calls

    def __enter__(self):
        self.start()

    def __exit__(self, *args):
        self.stop()
        self.reset()

    def activate(self, func):
        evaldict = {'ldap3mock': self, 'func': func}
        return get_wrapped(func, _wrapper_template, evaldict)

    def _on_Server(self, host, port,
                              use_ssl,
                              connect_timeout):
        # mangle request packet

        return "FakeServerObject"

    def _on_Connection(self, server, user, password,
                       auto_bind=None, client_strategy=None,
                       authentication=None, check_names=None,
                       auto_referrals=None):
        """
        We need to create a Connection object with
        methods:
            search()
            unbind()
        and object
            response
        """
        # check the password
        correct_password = False
        # Anonymous bind
        if authentication == ldap3.ANONYMOUS and user == "":
            correct_password = True
        for entry in self.directory:
            if entry.get("dn") == user:
                pw = entry.get("attributes").get("userPassword")
                if pw == password:
                    correct_password = True
        self.con_obj = Connection(self.directory)
        self.con_obj.bound = correct_password
        return self.con_obj

    def start(self):
        import mock

        def unbound_on_Server(host, port,
                              use_ssl,
                              connect_timeout, *a, **kwargs):
            return self._on_Server(host, port,
                              use_ssl,
                              connect_timeout, *a, **kwargs)
        self._patcher = mock.patch('ldap3.Server',
                                   unbound_on_Server)
        self._patcher.start()

        def unbound_on_Connection(server, user,
                                  password,
                                  auto_bind,
                                  client_strategy,
                                  authentication,
                                  check_names,
                                  auto_referrals, *a, **kwargs):
            return self._on_Connection(server, user,
                                       password,
                                       auto_bind,
                                       client_strategy,
                                       authentication,
                                       check_names,
                                       auto_referrals, *a,
                                       **kwargs)

        self._patcher2 = mock.patch('ldap3.Connection',
                                    unbound_on_Connection)
        self._patcher2.start()

    def stop(self):
        self._patcher.stop()
        self._patcher2.stop()

# expose default mock namespace
mock = _default_mock = Ldap3Mock()
__all__ = []
for __attr in (a for a in dir(_default_mock) if not a.startswith('_')):
    __all__.append(__attr)
    globals()[__attr] = getattr(_default_mock, __attr)
