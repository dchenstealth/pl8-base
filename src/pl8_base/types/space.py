# SPDX-License-Identifier: MIT

from types import MappingProxyType
from typing import ClassVar

from .base import BaseObject


class SpaceInfo(BaseObject):
    """
    Item representing a Space's metadata; a Space owns exactly this one row.

    space_id is caller-supplied rather than generated, so create_space
    conditions on the key being free and reports the clash instead of retrying
    into a new id. util.validate_space_id is what keeps a "#" out of it, and so
    what keeps both this PK and the ISSUE#{space_id}#{issue_id} it also
    composes unambiguous.

    Indexed into GSI1 under a single constant partition so every Space can be
    enumerated with one query, sorted by space_id. That partition is
    deliberately hot: it holds one small row per Space, reading it as a whole
    list is the entire point, and a Space count large enough to strain a
    partition would need a different enumeration story regardless.

    GSI1SK carries no numeric prefix. Prefixes order groups within a shared
    partition and this partition has one group, while "#" sorts below every
    letter, so a later SPACEMEMBER#... group would still land contiguous and
    after these without a retrofit.

    A Space holds no referential integrity over its Issues. An Issue may name a
    Space that does not exist, and deleting a Space leaves its Issues in place;
    see mixins/space.py.
    """
    KEY_ATTRS: ClassVar[MappingProxyType] = MappingProxyType({
        "PK": "SPACE#{space_id}",
        "SK": "100#INFO",
        "GSI1PK": "SPACES",
        "GSI1SK": "SPACE#{space_id}",
    })
    COMPRESSED_ATTRS: ClassVar[set[str]] = {"description"}

    PK: str | None = None
    SK: str | None = None
    GSI1PK: str | None = None
    GSI1SK: str | None = None
    type_version: str = "0.0.1"

    space_id: str
    name: str
    description: str
