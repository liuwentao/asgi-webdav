from typing import Callable, Optional, OrderedDict
from dataclasses import dataclass, field
from uuid import UUID
from urllib.parse import urlparse

import xmltodict
from pyexpat import ExpatError

from asgi_webdav.constants import (
    DAV_METHODS,
    DAVMethod,
    DAVPropertyIdentity,
    DAVPropertyPatches,
    DAVPath,
    DAVLockScope,
)
from asgi_webdav.helpers import receive_all_data_in_one_call
from asgi_webdav.exception import NotASGIRequestException


@dataclass
class DAVRequest:
    """Information from Request
    DAVDistributor => DavProvider => provider.implement
    """
    scope: dict
    receive: Callable
    send: Callable

    # header's info
    method: str = field(init=False)
    headers: dict[bytes] = field(init=False)
    src_path: DAVPath = field(init=False)
    dst_path: Optional[DAVPath] = None
    depth: int = -1  # default's infinity
    overwrite: bool = field(init=False)
    timeout: int = field(init=False)

    # body's info
    body: bytes = field(init=False)
    body_is_parsed_success: bool = False
    # propfind_keys is None ===> request propname TODO!!!
    # len(propfind_keys) == 0 ===> allprop
    propfind_keys: Optional[set[str]] = field(default_factory=set)
    propfind_entries: list[DAVPropertyIdentity] = field(default_factory=list)
    proppatch_entries: list[DAVPropertyPatches] = field(default_factory=list)

    # lock info(in both header and body)
    lock_scope: Optional[DAVLockScope] = None
    lock_owner: Optional[str] = None
    lock_token: Optional[UUID] = None
    lock_token_is_parsed_success: bool = True

    def __post_init__(self):
        self.method = self.scope.get('method')
        if self.method not in DAV_METHODS:
            raise NotASGIRequestException(
                'method:{} is not support method'.format(self.method)
            )

        self.headers = dict(self.scope.get('headers'))

        # path
        raw_path = self.scope.get('path')
        self.src_path = DAVPath(raw_path)
        raw_path = self.headers.get(b'destination')
        if raw_path:
            self.dst_path = DAVPath(urlparse(raw_path).path)

        # depth
        """
        https://tools.ietf.org/html/rfc4918
        14.4.  depth XML Element
        Name:   depth
        Purpose:   Used for representing depth values in XML content (e.g.,
          in lock information).
        Value:   "0" | "1" | "infinity"
        <!ELEMENT depth (#PCDATA) >
        """
        depth = self.headers.get(b'depth')
        if depth is None:
            # default' value
            pass

        elif depth == b'infinity':
            self.depth = -1

        else:
            try:
                depth = int(depth)
                if depth < 0:
                    raise ValueError

                self.depth = depth
            except ValueError:
                raise ExpatError('bad depth:{}'.format(depth))

        # overwrite
        """
        https://tools.ietf.org/html/rfc4918#page-77
        10.6.  Overwrite Header
              Overwrite = "Overwrite" ":" ("T" | "F")
        """
        if self.headers.get(b'overwrite', b'F') == b'F':
            self.overwrite = False
        else:
            self.overwrite = True

        # timeout
        """
        https://tools.ietf.org/html/rfc4918#page-78
        10.7.  Timeout Request Header
        
              TimeOut = "Timeout" ":" 1#TimeType
              TimeType = ("Second-" DAVTimeOutVal | "Infinite")
                         ; No LWS allowed within TimeType
              DAVTimeOutVal = 1*DIGIT
        
           Clients MAY include Timeout request headers in their LOCK requests.
           However, the server is not required to honor or even consider these
           requests.  Clients MUST NOT submit a Timeout request header with any
           method other than a LOCK method.
        
           The "Second" TimeType specifies the number of seconds that will
           elapse between granting of the lock at the server, and the automatic
           removal of the lock.  The timeout value for TimeType "Second" MUST
           NOT be greater than 2^32-1.
        
           See Section 6.6 for a description of lock timeout behavior.        
        """
        timeout = self.headers.get(b'timeout')
        if timeout:
            self.timeout = int(timeout[7:])
        else:
            # TODO ??? default value??
            self.timeout = 0

        # if
        if_lock_tokens = list()
        if_lock_token = self.headers.get(b'if')
        if if_lock_token:
            # print('if: {}'.format(if_lock_token))
            if_lock_tokens = self._parser_lock_token(if_lock_token)
            if len(if_lock_tokens) == 0:
                self.lock_token_is_parsed_success = False

        # lock-token
        lock_tokens = list()
        lock_token = self.headers.get(b'lock-token')
        if lock_token:
            # print('lock-token: {}'.format(lock_token))
            lock_tokens = self._parser_lock_token(lock_token)
            if len(lock_tokens) == 0:
                self.lock_token_is_parsed_success = False

        lock_tokens += if_lock_tokens
        if len(lock_tokens) > 0:
            # print('tokens:', lock_tokens)
            self.lock_token = lock_tokens[0]  # TODO!!!

        return

    @staticmethod
    def _parser_lock_token(data: bytes) -> list[UUID]:
        tokens = list()
        for x in data.split(b'('):
            x = x.rstrip(b' >)')
            index = x.rfind(b':')
            if index == -1:
                continue

            x = x[index + 1:]
            try:
                token = UUID(str(x, encoding='utf-8'))
                tokens.append(token)
            except ValueError:
                pass

        return tokens

    @staticmethod
    def _parser_xml_data(data: bytes) -> Optional[OrderedDict]:
        try:
            data = xmltodict.parse(data, process_namespaces=True)

        except ExpatError:
            # TODO
            return None

        return data

    @staticmethod
    def _cut_ns_key(ns_key: str) -> tuple[str, str]:
        index = ns_key.rfind(':')
        if index == -1:
            return '', ns_key
        else:
            return ns_key[:index], ns_key[index + 1:]

    async def _parser_body_propfind(self) -> bool:
        self.body = await receive_all_data_in_one_call(self.receive)
        """A client may choose not to submit a request body.  An empty PROPFIND
           request body MUST be treated as if it were an 'allprop' request.
        """
        if len(self.body) == 0:
            self.propfind_keys = set()
            return True

        data = self._parser_xml_data(self.body)
        if data is None:
            return False

        find_symbol = 'DAV::propfind'
        if 'propname' in data[find_symbol]:
            self.propfind_keys = None
            return True

        if 'DAV::allprop' in data[find_symbol]:
            self.propfind_keys = set()
            return True

        if 'DAV::prop' not in data[find_symbol]:
            # TODO error
            return False

        for ns_key in data[find_symbol]['DAV::prop']:
            ns, key = self._cut_ns_key(ns_key)
            self.propfind_keys.add(key)
            self.propfind_entries.append((ns, key))

        # TODO default is propfind ??
        return True

    async def _parser_body_proppatch(self) -> bool:
        self.body = await receive_all_data_in_one_call(self.receive)
        data = self._parser_xml_data(self.body)
        if data is None:
            return False

        update_symbol = 'DAV::propertyupdate'
        for action in data[update_symbol]:
            _, key = self._cut_ns_key(action)
            if key == 'set':
                method = True
            else:
                method = False

            for item in data[update_symbol][action]:
                if isinstance(item, OrderedDict):
                    ns_key, value = item['DAV::prop'].popitem()
                else:
                    ns_key, value = data[update_symbol][action][
                        item].popitem()
                    if isinstance(value, OrderedDict):
                        # value namespace: drop namespace info # TODO ???
                        value, _ = value.popitem()
                        _, value = self._cut_ns_key(value)
                        # value = "<{} xmlns='{}'>".format(vns_key, vns_ns)

                ns, key = self._cut_ns_key(ns_key)
                if not isinstance(value, str):
                    value = str(value)

                self.proppatch_entries.append(((ns, key), value, method))

        return True

    async def _parser_body_lock(self) -> bool:
        self.body = await receive_all_data_in_one_call(self.receive)
        if len(self.body) == 0:
            # LOCK accept empty body
            return True

        data = self._parser_xml_data(self.body)
        if data is None:
            return False

        # print(data)
        if 'DAV::exclusive' in data['DAV::lockinfo']['DAV::lockscope']:
            self.lock_scope = DAVLockScope.exclusive
        else:
            self.lock_scope = DAVLockScope.shared

        lock_owner = data['DAV::lockinfo']['DAV::owner']
        self.lock_owner = str(lock_owner)
        return True

    async def parser_body(self) -> bool:
        if self.method == DAVMethod.PROPFIND:
            self.body_is_parsed_success = await self._parser_body_propfind()

        elif self.method == DAVMethod.PROPPATCH:
            self.body_is_parsed_success = await self._parser_body_proppatch()

        elif self.method == DAVMethod.LOCK:
            self.body_is_parsed_success = await self._parser_body_lock()

        else:
            self.body_is_parsed_success = False

        return self.body_is_parsed_success

    def __repr__(self):
        simple_fields = ['method', 'src_path']
        rich_fields = list()

        if self.method == DAVMethod.PROPFIND:
            simple_fields += [
                'body_is_parsed_success', 'depth', 'propfind_keys'
            ]
            rich_fields += ['propfind_entries', ]

        elif self.method == DAVMethod.PROPPATCH:
            simple_fields += ['body_is_parsed_success', 'depth']
            rich_fields += ['proppatch_entries', ]

        elif self.method in (DAVMethod.COPY, DAVMethod.MOVE):
            simple_fields += ['dst_path', 'depth', 'overwrite']

        elif self.method in (DAVMethod.LOCK, DAVMethod.UNLOCK):
            simple_fields += [
                'body_is_parsed_success', 'depth', 'timeout',
                'lock_scope', 'lock_token', 'lock_owner'
            ]

        simple = '|'.join(
            [str(self.__getattribute__(name)) for name in simple_fields]
        )

        try:
            from prettyprinter import pformat
            scope = pformat(self.scope)
            rich = '\n'.join(
                [pformat(self.__getattribute__(name)) for name in rich_fields]
            )
            s = '{}\n{}\n{}'.format(simple, scope, rich)

        except ImportError:
            s = simple

        return s
