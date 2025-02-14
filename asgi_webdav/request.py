from typing import Callable, Optional, OrderedDict
from dataclasses import dataclass, field
from uuid import UUID
from urllib.parse import (
    urlparse,
    unquote as decode_path_name_for_url,
)

import xmltodict
from pyexpat import ExpatError

from asgi_webdav.constants import (
    DAV_METHODS,
    DAVMethod,
    DAVPath,
    DAVDepth,
    DAVLockScope,
    DAV_PROPERTY_BASIC_KEYS,
    DAVPropertyIdentity,
    DAVPropertyPatches,
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

    # header's info ---
    method: str = field(init=False)
    headers: dict[bytes] = field(init=False)
    src_path: DAVPath = field(init=False)
    dst_path: Optional[DAVPath] = None
    depth: DAVDepth = DAVDepth.infinity
    overwrite: bool = field(init=False)
    timeout: int = field(init=False)

    # body's info ---
    body: bytes = field(init=False)
    body_is_parsed_success: bool = False

    # propfind info ---
    propfind_only_fetch_property_name: bool = False  # TODO!!!

    propfind_fetch_all_property: bool = True
    propfind_only_fetch_basic: bool = False
    propfind_basic_keys: set[str] = field(default_factory=set)
    propfind_extra_keys: list[DAVPropertyIdentity] = field(default_factory=list)

    # proppatch info ---
    proppatch_entries: list[DAVPropertyPatches] = field(default_factory=list)

    # lock info --- (in both header and body)
    lock_scope: Optional[DAVLockScope] = None
    lock_owner: Optional[str] = None
    lock_token: Optional[UUID] = None
    lock_token_path: Optional[DAVPath] = None  # from header.If
    lock_token_etag: Optional[str] = None
    lock_token_is_parsed_success: bool = True

    # distribute information
    dist_prefix: Optional[DAVPath] = None
    dist_src_path: Optional[DAVPath] = None
    dist_dst_path: Optional[DAVPath] = None

    def __post_init__(self):
        self.method = self.scope.get("method")
        if self.method not in DAV_METHODS:
            raise NotASGIRequestException(
                "method:{} is not support method".format(self.method)
            )

        self.headers = dict(self.scope.get("headers"))

        # path
        raw_path = self.scope.get("path")
        self.src_path = DAVPath(decode_path_name_for_url(raw_path))
        raw_path = self.headers.get(b"destination")
        if raw_path:
            self.dst_path = DAVPath(decode_path_name_for_url(urlparse(raw_path).path))

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
        depth = self.headers.get(b"depth")
        if depth is None:
            # default' value
            pass

        elif depth == b"infinity":
            self.depth = DAVDepth.infinity

        else:
            try:
                depth = int(depth)
                if depth == 0:
                    self.depth = DAVDepth.d0
                elif depth == 1:
                    self.depth = DAVDepth.d1
                else:
                    raise ValueError

            except ValueError:
                raise ExpatError("bad depth:{}".format(depth))

        # overwrite
        """
        https://tools.ietf.org/html/rfc4918#page-77
        10.6.  Overwrite Header
              Overwrite = "Overwrite" ":" ("T" | "F")
        """
        if self.headers.get(b"overwrite", b"F") == b"F":
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
        timeout = self.headers.get(b"timeout")
        if timeout:
            self.timeout = int(timeout[7:])
        else:
            # TODO ??? default value??
            self.timeout = 0

        # header: if
        header_if = self.headers.get(b"if")
        if header_if:
            lock_tokens_from_if = self._parser_header_if(header_if.decode("utf-8"))
            if len(lock_tokens_from_if) == 0:
                self.lock_token_is_parsed_success = False
            else:
                self.lock_token = lock_tokens_from_if[0][0]
                self.lock_token_etag = lock_tokens_from_if[0][1]

        # header: lock-token
        header_lock_token = self.headers.get(b"lock-token")
        if header_lock_token:
            lock_token = self._parser_lock_token_str(header_lock_token.decode("utf-8"))
            if lock_token is None:
                self.lock_token_is_parsed_success = False
            else:
                self.lock_token = lock_token

        return

    @staticmethod
    def _take_string_from_brackets(data: str, start: str, end: str) -> Optional[str]:
        begin_index = data.find(start)
        end_index = data.find(end)

        if begin_index == -1 or end_index == -1:
            return None

        return data[begin_index + 1 : end_index]

    def _parser_lock_token_str(self, data: str) -> Optional[UUID]:
        data = self._take_string_from_brackets(data, "<", ">")
        if data is None:
            return None

        index = data.rfind(":")
        if index == -1:
            return None

        token = data[index + 1 :]
        try:
            token = UUID(token)
        except ValueError:
            return None

        return token

    def _parser_header_if(self, data: str) -> list[tuple[UUID, Optional[str]]]:
        """
        b'if',
        b'<http://192.168.200.198:8000/litmus/lockcoll/> '
            b'(<opaquelocktoken:245ec6a9-e8e2-4c7d-acd4-740b9e301ae0> '
            b'[e24bfe34b6750624571283fcf1ed8542]) '
            b(Not <DAV:no-lock> '
            b'[e24bfe34b6750624571283fcf1ed8542])'
        """
        begin_index = data.find("(")
        if begin_index != -1:
            lock_token_path = data[:begin_index]

            lock_token_path = self._take_string_from_brackets(lock_token_path, "<", ">")
            if lock_token_path:
                lock_token_path = urlparse(lock_token_path).path
                if len(lock_token_path) != 0:
                    self.lock_token_path = DAVPath(lock_token_path)

        tokens = list()
        while True:
            begin_index = data.find("(")
            end_index = data.find(")")
            if begin_index == -1 or end_index == -1:
                break

            block = data[begin_index + 1 : end_index]
            # block = self._take_string_from_brackets(data)

            if not block.startswith("Not"):
                token = self._parser_lock_token_str(block)
                etag = self._take_string_from_brackets(block, "[", "]")

                if token is None:
                    self.lock_token_is_parsed_success = False
                else:
                    tokens.append((token, etag))

            data = data[end_index + 1 :]

        return tokens

    def update_distribute_info(self, dist_prefix: DAVPath):
        self.dist_prefix = dist_prefix
        self.dist_src_path = self.src_path.get_child(dist_prefix)
        if self.dst_path:
            self.dist_dst_path = self.dst_path.get_child(dist_prefix)

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
        index = ns_key.rfind(":")
        if index == -1:
            return "", ns_key
        else:
            return ns_key[:index], ns_key[index + 1 :]

    async def _parser_body_propfind(self) -> bool:
        self.body = await receive_all_data_in_one_call(self.receive)
        """
        A client may choose not to submit a request body.  An empty PROPFIND
           request body MUST be treated as if it were an 'allprop' request.
        """
        if len(self.body) == 0:
            # allprop
            return True

        data = self._parser_xml_data(self.body)
        if data is None:
            return False

        find_symbol = "DAV::propfind"
        if "propname" in data[find_symbol]:
            self.propfind_only_fetch_property_name = True
            return True

        if "DAV::allprop" in data[find_symbol]:
            return True
        else:
            self.propfind_fetch_all_property = False

        if "DAV::prop" not in data[find_symbol]:
            # TODO error
            return False

        for ns_key in data[find_symbol]["DAV::prop"]:
            ns, key = self._cut_ns_key(ns_key)
            if key in DAV_PROPERTY_BASIC_KEYS:
                self.propfind_basic_keys.add(key)
            else:
                self.propfind_extra_keys.append((ns, key))

        if len(self.propfind_extra_keys) == 0:
            self.propfind_only_fetch_basic = True

        return True

    async def _parser_body_proppatch(self) -> bool:
        self.body = await receive_all_data_in_one_call(self.receive)
        data = self._parser_xml_data(self.body)
        if data is None:
            return False

        update_symbol = "DAV::propertyupdate"
        for action in data[update_symbol]:
            _, key = self._cut_ns_key(action)
            if key == "set":
                method = True
            else:
                method = False

            for item in data[update_symbol][action]:
                if isinstance(item, OrderedDict):
                    ns_key, value = item["DAV::prop"].popitem()
                else:
                    ns_key, value = data[update_symbol][action][item].popitem()
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
        if "DAV::exclusive" in data["DAV::lockinfo"]["DAV::lockscope"]:
            self.lock_scope = DAVLockScope.exclusive
        else:
            self.lock_scope = DAVLockScope.shared

        lock_owner = data["DAV::lockinfo"]["DAV::owner"]
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
        simple_fields = ["method", "src_path"]
        rich_fields = list()

        if self.method == DAVMethod.PROPFIND:
            simple_fields += [
                "body_is_parsed_success",
                "depth",
                "propfind_only_fetch_property_name",
                "propfind_fetch_all_property",
                "propfind_only_fetch_basic",
                "propfind_basic_keys",
            ]
            rich_fields += [
                "propfind_extra_keys",
            ]

        elif self.method == DAVMethod.PROPPATCH:
            simple_fields += ["body_is_parsed_success", "depth"]
            rich_fields += [
                "proppatch_entries",
            ]

        elif self.method == DAVMethod.PUT:
            simple_fields += [
                "lock_token",
                "lock_token_path",
                "lock_token_is_parsed_success",
            ]

        elif self.method in (DAVMethod.COPY, DAVMethod.MOVE):
            simple_fields += ["dst_path", "depth", "overwrite"]

        elif self.method in (DAVMethod.LOCK, DAVMethod.UNLOCK):
            simple_fields += [
                "body_is_parsed_success",
                "depth",
                "timeout",
                "lock_scope",
                "lock_owner",
                "lock_token",
                "lock_token_path",
                "lock_token_is_parsed_success",
            ]

        simple = "|".join([str(self.__getattribute__(name)) for name in simple_fields])

        try:
            from prettyprinter import pformat

            scope = pformat(self.scope)
            rich = "\n".join(
                [pformat(self.__getattribute__(name)) for name in rich_fields]
            )
            s = "{}\n{}\n{}".format(simple, scope, rich)

        except ImportError:
            s = simple

        return s
