from types import MappingProxyType
from typing import ClassVar

from .base import BaseObject
from .enums import IssueStatus


class IssueInfo(BaseObject):
    """
    Item representing an Issue's metadata; all Issues must have an IssueInfo row.
    Indexed into GSI1 by status, sorted by created_at to allow querying by most
    recently created.
    """
    KEY_ATTRS: ClassVar[MappingProxyType] = MappingProxyType({
        "PK": "ISSUE#{space_id}#{issue_id}",
        "SK": "100#INFO",
        "GSI1PK": "ISSUESPACESTATUS#{space_id}#{status}",
        "GSI1SK": "CREATED#{created_at}#ISSUE#{issue_id}",
    })
    COMPRESSED_ATTRS: ClassVar[set[str]] = {"description"}

    PK: str = None
    SK: str = None
    GSI1PK: str = None
    GSI1SK: str = None
    type_version: str = "0.0.1"

    issue_id: str
    space_id: str
    title: str
    description: str
    assignee: str
    status: IssueStatus
    status_updated_at: str = None

    # The number of IssueBlockers blocking this Issue with is_blocking_issue_done=False
    # MUST be enforced atomically via transactions.
    num_active_blockers: int = 0

    def __post_init__(self):
        super().__post_init__()

        if not self.status_updated_at:
            self.status_updated_at = self.created_at

    @property
    def is_done(self):
        return self.status == IssueStatus.DONE


class IssueBlocker(BaseObject):
    """
    Item representing a "blocking" Issue that blocks a "blocked" Issue.
    is_blocking_issue_done SHOULD be updated when the blocking Issue is transitioned
    to DONE or out of DONE, but may done asynchronously.

    Indexed into the blocking Issue's PK collection. IssueBlockers MAY reference a
    deleted blocked Issue, but MUST NOT reference a deleted blocking Issue.
    """
    KEY_ATTRS: ClassVar[MappingProxyType] = MappingProxyType({
        "PK": "ISSUE#{blocking_issue_space_id}#{blocking_issue_id}",
        "SK": "800#BLOCKEDISSUE#{blocked_issue_space_id}#{blocked_issue_id}",
        "GSI1PK": "BLOCKEDISSUE#{blocked_issue_space_id}#{blocked_issue_id}",
        "GSI1SK": "500#BLOCKINGISSUE#{blocking_issue_space_id}#{blocking_issue_id}",
    })
    COMPRESSED_ATTRS: ClassVar[set[str]] = set()

    PK: str = None
    SK: str = None
    GSI1PK: str = None
    GSI1SK: str = None
    type_version: str = "0.0.1"

    blocking_issue_space_id: str
    blocking_issue_id: str
    blocked_issue_space_id: str
    blocked_issue_id: str
    is_blocking_issue_done: bool
