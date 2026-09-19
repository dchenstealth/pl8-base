from .enums import IssueStatus
from .events import (
    BaseEvent,
    IssueNumActiveBlockersZeroed,
    IssueDeleted,
    IssueDone,
    IssueReady,
)
from .issue import IssueInfo, IssueBlocker
from .space import SpaceInfo


CLASS_MAP = {
    "IssueInfo": IssueInfo,
    "IssueBlocker": IssueBlocker,
    "SpaceInfo": SpaceInfo,
}

EVENT_CLASS_MAP = {
    "IssueNumActiveBlockersZeroed": IssueNumActiveBlockersZeroed,
    "IssueDeleted": IssueDeleted,
    "IssueDone": IssueDone,
    "IssueReady": IssueReady,
}

__all__ = [
    "IssueStatus",
    "CLASS_MAP",
    "EVENT_CLASS_MAP",
    "BaseEvent",
    *CLASS_MAP.keys(),
    *EVENT_CLASS_MAP.keys(),
]
