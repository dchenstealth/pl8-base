# SPDX-License-Identifier: MIT

import abc
import gzip

import msgspec

from boto3.dynamodb.types import TypeSerializer, TypeDeserializer

from ..errors import DDBArgsError
from ..util import cleanup_decimals, isotime


class BaseObjectMeta(msgspec.StructMeta, abc.ABCMeta):
    def __new__(mcls, name, bases, namespace, **struct_config):
        struct_config.setdefault("kw_only", True)
        return super().__new__(mcls, name, bases, namespace, **struct_config)


# tag_field "type" sets the class name as the `type` field
# when serializing to dict
class BaseObject(msgspec.Struct, tag=True, tag_field="type",
                 metaclass=BaseObjectMeta):
    # SemVer string for type versioning, e.g. 0.0.1
    type_version: str

    # Resolved in __post_init__ when not supplied, so None only ever appears
    # between __init__ and that call, never on a constructed object.
    created_at: str | None = None
    updated_at: str | None = None
    version: int = 1

    @property
    @abc.abstractmethod
    def KEY_ATTRS(self):
        # Defined as class attr on subclasses. MappingProxyType (dict)
        # of key attrs to their format strings. Example:
        # MappingProxyType({
        #     "PK": "ITEM#{item_id}",
        #     "SK": "#INFO",
        # })
        pass

    @property
    @abc.abstractmethod
    def COMPRESSED_ATTRS(self):
        # Defined as a class attr on subclasses. set of key attrs to
        # gzip compress on serialization, decompress on deserialization
        pass

    @classmethod
    def from_item(cls, item, *, td=None):
        if td is None:
            td = TypeDeserializer()

        deserialized = {k: td.deserialize(v) for k, v in item.items()}
        deserialized = cleanup_decimals(deserialized)

        for k in cls.COMPRESSED_ATTRS:
            if k in deserialized:
                val = gzip.decompress(deserialized[k].value).decode()
                deserialized[k] = val

        return msgspec.convert(deserialized, cls)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = isotime()

        if not self.updated_at:
            self.updated_at = self.created_at

        # Initialize key attrs if not set on object.
        # Called when creating object; initializing from db object
        # directly sets key attrs.
        for attr, format_str in self.KEY_ATTRS.items():
            if getattr(self, attr) is None:
                val = format_str.format(**self.dict())
                setattr(self, attr, val)

    def dict(self):
        return msgspec.to_builtins(self)

    @classmethod
    def compress_value(cls, attr, value):
        """Compress one attr value if the attr is compressed on this type.

        Partial writes need the same treatment a full serialize() gives, so
        both go through here rather than each gzipping on their own.

        Args:
            attr (str): attribute name
            value: attribute value

        Returns:
            bytes or original value: gzipped bytes for a compressed attr,
                otherwise the value unchanged

        Raises:
            DDBArgsError: if a compressed attr is not a string
        """
        if attr not in cls.COMPRESSED_ATTRS:
            return value

        if not isinstance(value, str):
            raise DDBArgsError("Compressed fields must be strings")

        return gzip.compress(value.encode())

    def serialize(self, *, ts=None):
        if ts is None:
            ts = TypeSerializer()

        return {k: ts.serialize(self.compress_value(k, v))
                for k, v in self.dict().items()}

    def serialized_pk(self, *, ts=None):
        if ts is None:
            ts = TypeSerializer()

        return {
            "PK": ts.serialize(self.PK),
            "SK": ts.serialize(self.SK),
        }
