# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

from .enums import IssueStatus
from .events import (
    BaseEvent,
    IssueDeleted,
    IssueDone,
    IssueNumActiveBlockersZeroed,
    IssueReady,
)
from .issue import IssueBlocker, IssueComment, IssueInfo
from .space import SpaceInfo

CLASS_MAP = {
    "IssueInfo": IssueInfo,
    "IssueBlocker": IssueBlocker,
    "IssueComment": IssueComment,
    "SpaceInfo": SpaceInfo,
}

EVENT_CLASS_MAP = {
    "IssueNumActiveBlockersZeroed": IssueNumActiveBlockersZeroed,
    "IssueDeleted": IssueDeleted,
    "IssueDone": IssueDone,
    "IssueReady": IssueReady,
}

# Spelled out rather than unpacked from the maps above: a comprehension or
# a *keys() splat is not statically readable, so type checkers and editors
# cannot resolve what this package exports. The test suite asserts this list
# and the two maps stay in agreement.
__all__ = [
    "CLASS_MAP",
    "EVENT_CLASS_MAP",
    "BaseEvent",
    "IssueBlocker",
    "IssueComment",
    "IssueDeleted",
    "IssueDone",
    "IssueInfo",
    "IssueNumActiveBlockersZeroed",
    "IssueReady",
    "IssueStatus",
    "SpaceInfo",
]
