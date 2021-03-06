# -*- coding: utf-8 -*-
#  Copyright (C) 2014 Cornelius Kölbel
#  contact:  corny@cornelinux.de
#
#  2016-02-22 Salvo Rapisarda
#             Allow objectGUID to be a users attribute
#  2016-02-19 Cornelius Kölbel <cornelius.koelbel@netknights.it>
#             Allow objectGUID to be the uid.
#  2015-10-05 Cornelius Kölbel <cornelius.koelbel@netknights.it>
#             Remove reverse_map, so that one LDAP field can map
#             to several privacyIDEA fields.
#  2015-04-16 Cornelius Kölbel <cornelius.koelbel@netknights.it>
#             Add redundancy with LDAP3 Server pools. Round Robin Strategy
#  2015-04-15 Cornelius Kölbel <cornelius.koelbel@netknights.it>
#             Increase test coverage
#  2014-12-25 Cornelius Kölbel <cornelius@privacyidea.org>
#             Rewrite for flask migration
#
# This code is free software; you can redistribute it and/or
# modify it under the terms of the GNU AFFERO GENERAL PUBLIC LICENSE
# License as published by the Free Software Foundation; either
# version 3 of the License, or any later version.
#
# This code is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU AFFERO GENERAL PUBLIC LICENSE for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
__doc__ = """This is the resolver to find users in LDAP directories like
OpenLDAP and Active Directory.

The file is tested in tests/test_lib_resolver.py
"""

import logging
import ldap3
import yaml
import traceback
import uuid
from ldap3.utils.conv import escape_bytes
import datetime

from UserIdResolver import UserIdResolver
from gettext import gettext as _
from privacyidea.lib.utils import to_utf8


log = logging.getLogger(__name__)
ENCODING = "utf-8"
# 1 sec == 10^9 nano secs == 10^7 * (100 nano secs)
MS_AD_MULTIPLYER = 10 ** 7
MS_AD_START = datetime.datetime(1601, 1, 1)


def get_ad_timestamp_now():
    """
    returns the current UTC time as it is used in Active Directory in the
    attribute accountExpires.
    This is 100-nano-secs since 1.1.1601

    :return: time
    :rtype: int
    """
    utc_now = datetime.datetime.utcnow()
    elapsed_time = utc_now - MS_AD_START
    total_seconds = elapsed_time.total_seconds()
    # convert this to (100 nanoseconds)
    return int(MS_AD_MULTIPLYER * total_seconds)


class AUTHTYPE(object):
    SIMPLE = "Simple"
    SASL_DIGEST_MD5 = "SASL Digest-MD5"
    NTLM = "NTLM"


class IdResolver (UserIdResolver):

    def __init__(self):
        self.i_am_bound = False
        self.uri = ""
        self.basedn = ""
        self.binddn = ""
        self.bindpw = ""
        self.timeout = 5.0  # seconds!
        self.sizelimit = 500
        self.loginname_attribute = ""
        self.searchfilter = ""
        self.reversefilter = ""
        self.userinfo = {}
        self.uidtype = ""
        self.noreferrals = False
        self.certificate = ""
        self.resolverId = self.uri
        self.scope = ldap3.SUBTREE

    def checkPass(self, uid, password):
        """
        This function checks the password for a given uid.
        - returns true in case of success
        -         false if password does not match
        
        """
        if self.authtype == AUTHTYPE.NTLM:  # pragma: no cover
            # fetch the PreWindows 2000 Domain from the self.binddn
            # which would be of the format DOMAIN\username and compose the
            # bind_user to DOMAIN\sAMAcountName
            domain_name = self.binddn.split('\\')[0]
            uinfo = self.getUserInfo(uid)
            # In fact we need the sAMAccountName. If the username mapping is
            # another attribute than the sAMAccountName the authentication
            # will fail!
            bind_user = "{0!s}\{1!s}".format(domain_name, uinfo.get("username"))
        else:
            bind_user = self._getDN(uid)

        server_pool = self.get_serverpool(self.uri, self.timeout)
        password = to_utf8(password)

        try:
            log.debug("Authtype: {0!s}".format(self.authtype))
            log.debug("user    : {0!s}".format(bind_user))
            # Whatever happens. If we have an empty bind_user, we must break
            # since we must avoid anonymous binds!
            if not bind_user or len(bind_user) < 1:
                raise Exception("No valid user. Empty bind_user.")
            l = self.create_connection(authtype=self.authtype,
                                       server=server_pool,
                                       user=bind_user,
                                       password=password,
                                       auto_referrals=not self.noreferrals)
            l.open()
            r = l.bind()
            log.debug("bind result: {0!s}".format(r))
            if not r:
                raise Exception("Wrong credentials")
            log.debug("bind seems successful.")
            l.unbind()
            log.debug("unbind successful.")
        except Exception as e:
            log.warning("failed to check password for {0!r}/{1!r}: {2!r}".format(uid, bind_user, e))
            return False
        
        return True

    def _trim_result(self, result_list):
        """
        The resultlist can contain entries of type:searchResEntry and of
        type:searchResRef. If self.noreferrals is true, all type:searchResRef
        will be removed.

        :param result_list: The result list of a LDAP search
        :type result_list: resultlist (list of dicts)
        :return: new resultlist
        """
        if self.noreferrals:
            new_list = []
            for result in result_list:
                if result.get("type") == "searchResEntry":
                    new_list.append(result)
                elif result.get("type") == "searchResRef":
                    # This is a Referral
                    pass
        else:
            new_list = result_list

        return new_list

    @staticmethod
    def _escape_loginname(loginname):
        """
        This function escapes the loginname according to
        https://msdn.microsoft.com/en-us/library/aa746475(v=vs.85).aspx
        This is to avoid username guessing by trying to login as user
           a*
           ac*
           ach*
           achm*
           achme*
           achemd*

        :param loginname: The loginname
        :return: The escaped loginname
        """
        return loginname.replace("\\", "\\5c").replace("*", "\\2a").replace(
            "(", "\\28").replace(")", "\\29").replace("/", "\\2f")

    @staticmethod
    def _get_uid(entry, uidtype):
        uid = None
        if uidtype.lower() == "dn":
           uid = entry.get("dn")
        else:
            attributes = entry.get("attributes")
            if type(attributes.get(uidtype)) == list:
                uid = attributes.get(uidtype)[0]
            else:
                uid = attributes.get(uidtype)
            # in case: fix the objectGUID
            if uidtype == "objectGUID":
                uid = str(uuid.UUID(bytes_le=uid))
        return uid
        
    def _getDN(self, userId):
        """
        This function returns the DN of a userId.
        Therefor it evaluates the self.uidtype.
        
        :param userId: The userid of a user
        :type userId: string
        
        :return: The DN of the object.
        """
        dn = ""
        if self.uidtype.lower() == "dn":
            dn = userId
        else:
            if self.uidtype == "objectGUID":
                userId = uuid.UUID("{{{0!s}}}".format(userId)).bytes_le
                userId = escape_bytes(userId)
            # get the DN for the Object
            self._bind()
            filter = "(&{0!s}({1!s}={2!s}))".format(self.searchfilter, self.uidtype, userId)
            self.l.search(search_base=self.basedn,
                          search_scope=self.scope,
                          search_filter=filter,
                          attributes=self.userinfo.values())
            r = self.l.response
            r = self._trim_result(r)
            if len(r) > 1:  # pragma: no cover
                raise Exception("Found more than one object for uid {0!r}".format(userId))
            if len(r) == 1:
                dn = r[0].get("dn")

        return dn
        
    def _bind(self):
        if not self.i_am_bound:
            server_pool = self.get_serverpool(self.uri, self.timeout)
            self.l = self.create_connection(authtype=self.authtype,
                                            server=server_pool,
                                            user=self.binddn,
                                            password=self.bindpw,
                                            auto_referrals=not self.noreferrals)
            self.l.open()
            #log.error("LDAP Server Pool States: %s" % server_pool.pool_states)
            if not self.l.bind():
                raise Exception("Wrong credentials")
            self.i_am_bound = True

    def getUserInfo(self, userId):
        """
        This function returns all user info for a given userid/object.
        
        :param userId: The userid of the object
        :type userId: string
        :return: A dictionary with the keys defined in self.userinfo
        :rtype: dict
        """
        ret = {}
        self._bind()
        
        if self.uidtype.lower() == "dn":
            # encode utf8, so that also german ulauts work in the DN
            self.l.search(search_base=to_utf8(userId),
                          search_scope=self.scope,
                          search_filter="(&" + self.searchfilter + ")",
                          attributes=self.userinfo.values())
        else:
            filter = "(&{0!s}({1!s}={2!s}))".format(self.searchfilter, self.uidtype, userId)
            self.l.search(search_base=self.basedn,
                              search_scope=self.scope,
                              search_filter=filter,
                              attributes=self.userinfo.values())

        r = self.l.response
        r = self._trim_result(r)
        if len(r) > 1:  # pragma: no cover
            raise Exception("Found more than one object for uid {0!r}".format(userId))

        for entry in r:
            attributes = entry.get("attributes")
            ret = self._ldap_attributes_to_user_object(attributes)

        return ret

    def _ldap_attributes_to_user_object(self, attributes):
        """
        This helper function converts the LDAP attributes to a dictionary for
        the privacyIDEA user. The LDAP Userinfo mapping is used to do so.

        :param attributes:
        :return: dict with privacyIDEA users.
        """
        ret = {}
        for ldap_k, ldap_v in attributes.items():
            for map_k, map_v in self.userinfo.items():
                if ldap_k == map_v:
                    if ldap_k == "objectGUID":
                        uuid_v = uuid.UUID(bytes_le=ldap_v[0])
                        ret[map_k] = str(uuid_v)
                    elif type(ldap_v) == list and map_k not in ["mobile"]:
                        # All lists (except) mobile return the first value as
                        #  a string. Mobile is returned as a list
                        ret[map_k] = ldap_v[0]
                    else:
                        ret[map_k] = ldap_v
        return ret
    
    def getUsername(self, user_id):
        """
        Returns the username/loginname for a given user_id
        :param user_id: The user_id in this resolver
        :type user_id: string
        :return: username
        :rtype: string
        """
        info = self.getUserInfo(user_id)
        return info.get('username', "")
   
    def getUserId(self, LoginName):
        """
        resolve the loginname to the userid.
        
        :param LoginName: The login name from the credentials
        :type LoginName: string
        :return: UserId as found for the LoginName
        """
        userid = ""
        self._bind()
        filter = "(&{0!s}({1!s}={2!s}))".format(self.searchfilter, self.loginname_attribute,
             self._escape_loginname(LoginName))

        # create search attributes
        attributes = self.userinfo.values()
        if self.uidtype.lower() != "dn":
            attributes.append(str(self.uidtype))
            
        self.l.search(search_base=self.basedn,
                      search_scope=self.scope,
                      search_filter=filter,
                      attributes=attributes)

        r = self.l.response
        r = self._trim_result(r)
        if len(r) > 1:  # pragma: no cover
            raise Exception("Found more than one object for Loginname {0!r}".format(
                            LoginName))
        
        for entry in r:
            userid = self._get_uid(entry, self.uidtype)

        return userid

    def getUserList(self, searchDict):
        """
        :param searchDict: A dictionary with search parameters
        :type searchDict: dict
        :return: list of users, where each user is a dictionary
        """
        ret = []
        self._bind()
        attributes = self.userinfo.values()
        ad_timestamp = get_ad_timestamp_now()
        if self.uidtype.lower() != "dn":
            attributes.append(str(self.uidtype))
            
        # do the filter depending on the searchDict
        filter = "(&" + self.searchfilter
        for search_key in searchDict.keys():
            if search_key == "accountExpires":
                comperator = ">="
                if searchDict[search_key] in ["1", 1]:
                    comperator = "<="
                filter += "(|({0!s}{1!s}{2!s})({3!s}!=0))".format(self.userinfo[search_key],
                                                  comperator,
                                                  get_ad_timestamp_now(),
                                                  self.userinfo[search_key])
            else:
                filter += "({0!s}={1!s})".format(self.userinfo[search_key], searchDict[search_key])
        filter += ")"

        g = self.l.extend.standard.paged_search(search_base=self.basedn,
                                                search_filter=filter,
                                                search_scope=self.scope,
                                                attributes=attributes,
                                                paged_size=100,
                                                size_limit=self.sizelimit,
                                                generator=True)
        # returns a generator of dictionaries
        for entry in g:
            # Simple fix for ignored sizelimit with Active Directory
            if len(ret) >= self.sizelimit:
                break
            try:
                attributes = entry.get("attributes")
                user = self._ldap_attributes_to_user_object(attributes)
                user['userid'] = self._get_uid(entry, self.uidtype)
                ret.append(user)
            except Exception as exx:  # pragma: no cover
                log.error("Error during fetching LDAP objects: {0!r}".format(exx))
                log.debug("{0!s}".format(traceback.format_exc()))

        return ret
    
    def getResolverId(self):
        """
        Returns the resolver Id
        This should be an Identifier of the resolver, preferable the type
        and the name of the resolver.
        """
        return self.uri

    @staticmethod
    def getResolverClassType():
        return 'ldapresolver'

    @staticmethod
    def getResolverDescriptor():
        return IdResolver.getResolverClassDescriptor()

    @staticmethod
    def getResolverType():
        return IdResolver.getResolverClassType()
    
    def loadConfig(self, config):
        """
        Load the config from conf.
        
        :param config: The configuration from the Config Table
        :type config: dict


        '#ldap_uri': 'LDAPURI',
        '#ldap_basedn': 'LDAPBASE',
        '#ldap_binddn': 'BINDDN',
        '#ldap_password': 'BINDPW',
        '#ldap_timeout': 'TIMEOUT',
        '#ldap_sizelimit': 'SIZELIMIT',
        '#ldap_loginattr': 'LOGINNAMEATTRIBUTE',
        '#ldap_searchfilter': 'LDAPSEARCHFILTER',
        '#ldap_userfilter': 'LDAPFILTER',
        '#ldap_mapping': 'USERINFO',
        '#ldap_uidtype': 'UIDTYPE',
        '#ldap_noreferrals' : 'NOREFERRALS',
        '#ldap_certificate': 'CACERTIFICATE',
                    
        """
        self.uri = config.get("LDAPURI")
        self.basedn = config.get("LDAPBASE")
        self.binddn = config.get("BINDDN")
        self.bindpw = config.get("BINDPW")
        self.timeout = float(config.get("TIMEOUT", 5))
        self.sizelimit = int(config.get("SIZELIMIT", 500))
        self.loginname_attribute = config.get("LOGINNAMEATTRIBUTE")
        self.searchfilter = config.get("LDAPSEARCHFILTER")
        self.reversefilter = config.get("LDAPFILTER")
        userinfo = config.get("USERINFO", "{}")
        self.userinfo = yaml.load(userinfo)
        self.uidtype = config.get("UIDTYPE", "DN")
        self.noreferrals = config.get("NOREFERRALS", False)
        self.certificate = config.get("CACERTIFICATE")
        self.scope = config.get("SCOPE") or ldap3.SUBTREE
        self.resolverId = self.uri
        self.authtype = config.get("AUTHTYPE", AUTHTYPE.SIMPLE)
        
        return self

    @staticmethod
    def split_uri(uri):
        """
        Splits LDAP URIs like:
        * ldap://server
        * ldaps://server
        * ldap[s]://server:1234
        * server
        :param uri: The LDAP URI
        :return: Returns a tuple of Servername, Port and SSL(bool)
        """
        port = None
        ssl = False
        ldap_elems = uri.split(":")
        if len(ldap_elems) == 3:
            server = ldap_elems[1].strip("/")
            port = int(ldap_elems[2])
            if ldap_elems[0].lower() == "ldaps":
                ssl = True
            else:
                ssl = False
        elif len(ldap_elems) == 2:
            server = ldap_elems[1].strip("/")
            port = None
            if ldap_elems[0].lower() == "ldaps":
                ssl = True
            else:
                ssl = False
        else:
            server = uri

        return server, port, ssl

    @classmethod
    def get_serverpool(cls, urilist, timeout):
        """
        This create the serverpool for the ldap3 connection.
        The URI from the LDAP resolver can contain a comma separated list of
        LDAP servers. These are split and then added to the pool.

        See
        https://github.com/cannatag/ldap3/blob/master/docs/manual/source/servers.rst#server-pool

        :param urilist: The list of LDAP URIs, comma separated
        :type urilist: basestring
        :param timeout: The connection timeout
        :type timeout: float
        :return: Server Pool
        :rtype: LDAP3 Server Pool Instance
        """
        strategy = ldap3.POOLING_STRATEGY_ROUND_ROBIN
        server_pool = ldap3.ServerPool(None, strategy, active=True,
                                       exhaust=True)
        for uri in urilist.split(","):
            uri = uri.strip()
            host, port, ssl = cls.split_uri(uri)
            server = ldap3.Server(host, port=port,
                                  use_ssl=ssl,
                                  connect_timeout=float(timeout))
            server_pool.add(server)
            log.debug("Added {0!s}, {1!s}, {2!s} to server pool.".format(host, port, ssl))
        return server_pool

    @classmethod
    def getResolverClassDescriptor(cls):
        """
        return the descriptor of the resolver, which is
        - the class name and
        - the config description

        :return: resolver description dict
        :rtype:  dict
        """
        descriptor = {}
        typ = cls.getResolverType()
        descriptor['clazz'] = "useridresolver.LDAPIdResolver.IdResolver"
        descriptor['config'] = {'LDAPURI': 'string',
                                'LDAPBASE': 'string',
                                'BINDDN': 'string',
                                'BINDPW': 'password',
                                'TIMEOUT': 'int',
                                'SIZELIMIT': 'int',
                                'LOGINNAMEATTRIBUTE': 'string',
                                'LDAPSEARCHFILTER': 'string',
                                'LDAPFILTER': 'string',
                                'USERINFO': 'string',
                                'UIDTYPE': 'string',
                                'NOREFERRALS': 'bool',
                                'CACERTIFICATE': 'string',
                                'AUTHTYPE': 'string'}
        return {typ: descriptor}

    @classmethod
    def testconnection(cls, param):
        """
        This function lets you test the to be saved LDAP connection.
        
        This is taken from controllers/admin.py
        
        :param param: A dictionary with all necessary parameter to test
                        the connection.
        :type param: dict
        
        :return: Tuple of success and a description
        :rtype: (bool, string)
        
        Parameters are:
            BINDDN, BINDPW, LDAPURI, TIMEOUT, LDAPBASE, LOGINNAMEATTRIBUTE,
            LDAPSEARCHFILTER,
            LDAPFILTER, USERINFO, SIZELIMIT, NOREFERRALS, CACERTIFICATE,
            AUTHTYPE
        """
        success = False
        uidtype = param.get("UIDTYPE")
        try:
            server_pool = cls.get_serverpool(param.get("LDAPURI"),
                                             float(param.get("TIMEOUT", 5)))
            l = cls.create_connection(authtype=param.get("AUTHTYPE",
                                                          AUTHTYPE.SIMPLE),
                                      server=server_pool,
                                      user=param.get("BINDDN"),
                                      password=to_utf8(param.get("BINDPW")),
                                      auto_referrals=not param.get(
                                           "NOREFERRALS"))
            l.open()
            #log.error("LDAP Server Pool States: %s" % server_pool.pool_states)
            if not l.bind():
                raise Exception("Wrong credentials")
            # create searchattributes
            attributes = yaml.load(param["USERINFO"]).values()
            if uidtype.lower() != "dn":
                attributes.append(str(uidtype))
            # search for users...
            g = l.extend.standard.paged_search(
                search_base=param["LDAPBASE"],
                search_filter="(&" + param["LDAPSEARCHFILTER"] + ")",
                search_scope=param.get("SCOPE") or ldap3.SUBTREE,
                attributes=attributes,
                paged_size=100,
                generator=True)
            # returns a generator of dictionaries
            count = 0
            uidtype_count = 0
            for entry in g:
                userid = cls._get_uid(entry, uidtype)
                count += 1
                if userid:
                    uidtype_count += 1
            if uidtype_count < count:  # pragma: no cover
                desc = _("Your LDAP config found %i user objects, but only %i "
                         "with the specified uidtype" % (count, uidtype_count))
            else:
                desc = _("Your LDAP config seems to be OK, %i user objects "
                         "found.") % count

            l.unbind()
            success = True
            
        except Exception as e:
            desc = "{0!r}".format(e)
        
        return success, desc

    @staticmethod
    def create_connection(authtype=None, server=None, user=None,
                          password=None, auto_bind=False,
                          client_strategy=ldap3.SYNC,
                          check_names=True,
                          auto_referrals=False):

        authentication = None
        if not user:
            authentication = ldap3.ANONYMOUS

        if authtype == AUTHTYPE.SIMPLE:
            if not authentication:
                authentication = ldap3.SIMPLE
            l = ldap3.Connection(server,
                                 user=user,
                                 password=to_utf8(password),
                                 auto_bind=auto_bind,
                                 client_strategy=client_strategy,
                                 authentication=authentication,
                                 check_names=check_names,
                                 auto_referrals=auto_referrals)
        elif authtype == AUTHTYPE.NTLM:  # pragma: no cover
            if not authentication:
                authentication = ldap3.NTLM
            l = ldap3.Connection(server,
                                 user=user,
                                 password=to_utf8(password),
                                 auto_bind=auto_bind,
                                 client_strategy=client_strategy,
                                 authentication=authentication,
                                 check_names=check_names,
                                 auto_referrals=auto_referrals)
        elif authtype == AUTHTYPE.SASL_DIGEST_MD5:  # pragma: no cover
            if not authentication:
                authentication = ldap3.SASL
            sasl_credentials = (str(user), str(password))
            l = ldap3.Connection(server,
                                 sasl_mechanism="DIGEST-MD5",
                                 sasl_credentials=sasl_credentials,
                                 auto_bind=auto_bind,
                                 client_strategy=client_strategy,
                                 authentication=authentication,
                                 check_names=check_names,
                                 auto_referrals=auto_referrals)
        else:
            raise Exception("Authtype {0!s} not supported".format(authtype))

        return l
