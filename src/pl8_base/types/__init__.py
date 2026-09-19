from .enums import IssueStatus
from .issue import IssueInfo, IssueBlocker
from .space import SpaceInfo


CLASS_MAP = {
    "IssueInfo": IssueInfo,
    "IssueBlocker": IssueBlocker,
    "SpaceInfo": SpaceInfo,
}

__all__ = [
    "IssueStatus",
    "CLASS_MAP",
    *CLASS_MAP.keys(),
]
