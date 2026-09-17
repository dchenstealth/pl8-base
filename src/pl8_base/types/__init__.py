from .enums import IssueStatus
from .issue import IssueInfo, IssueBlocker


CLASS_MAP = {
    "IssueInfo": IssueInfo,
    "IssueBlocker": IssueBlocker,
}

__all__ = [
    "IssueStatus",
    "CLASS_MAP",
    *CLASS_MAP.keys(),
]
