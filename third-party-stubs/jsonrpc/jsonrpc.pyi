# Stubs for jsonrpc.jsonrpc (Python 3)
#
# NOTE: This dynamically typed stub was automatically generated by stubgen.

import abc
from .jsonrpc1 import JSONRPC10Request
from .jsonrpc2 import JSONRPC20Request
from .utils import JSONSerializable
from typing import Any

class JSONRPCRequest(JSONSerializable, metaclass=abc.ABCMeta):
    @classmethod
    def from_json(cls, json_str: Any): ...
    @classmethod
    def from_data(cls, data: Any): ...
