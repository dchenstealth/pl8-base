# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

from types import MappingProxyType
from typing import ClassVar

from ..util import isotime
from .base import BaseObject
from .enums import IssueStatus


class IssueInfo(BaseObject):
    """
    Item representing an Issue's metadata; all Issues must have an IssueInfo row.
    Indexed into GSI1 by status, sorted by status_updated_at: ascending gives the
    Issues that have sat in the status longest, descending the most recently
    moved. A status transition already rewrites GSI1PK and so already reinserts
    the GSI entry, which is why keying the sort on status_updated_at rather than
    created_at costs no extra write.
    """
    KEY_ATTRS: ClassVar[MappingProxyType] = MappingProxyType({
        "PK": "ISSUE#{space_id}#{issue_id}",
        "SK": "100#INFO",
        "GSI1PK": "ISSUESPACESTATUS#{space_id}#{status}",
        "GSI1SK": "STATUSUPDATED#{status_updated_at}#ISSUE#{issue_id}",
    })
    COMPRESSED_ATTRS: ClassVar[set[str]] = {"description"}

    PK: str | None = None
    SK: str | None = None
    GSI1PK: str | None = None
    GSI1SK: str | None = None
    type_version: str = "0.0.1"

    issue_id: str
    space_id: str
    title: str
    description: str
    status: IssueStatus
    status_updated_at: str | None = None

    # The number of IssueBlockers blocking this Issue with is_blocking_issue_done=False
    # MUST be enforced atomically via transactions.
    num_active_blockers: int = 0

    def __post_init__(self):
        # status_updated_at feeds GSI1SK, so both it and created_at must be
        # resolved before BaseObject.__post_init__ renders KEY_ATTRS from
        # self.dict(). Setting created_at here also makes the base class skip it.
        if not self.created_at:
            self.created_at = isotime()

        if not self.status_updated_at:
            self.status_updated_at = self.created_at

        super().__post_init__()

    @property
    def is_done(self):
        return self.status == IssueStatus.DONE


class IssueBlocker(BaseObject):
    """
    Item representing a "blocking" Issue that blocks a "blocked" Issue.
    is_blocking_issue_done SHOULD be updated when the blocking Issue is transitioned
    to DONE, but may be done asynchronously. DONE is terminal, so the flag only ever
    moves False -> True; handle_issue_done relies on that to stay idempotent.

    Indexed into the blocking Issue's PK collection. An IssueBlocker MUST NOT
    reference a deleted Issue on either side; handle_issue_deleted sweeps both
    directions.
    """
    KEY_ATTRS: ClassVar[MappingProxyType] = MappingProxyType({
        "PK": "ISSUE#{blocking_issue_space_id}#{blocking_issue_id}",
        "SK": "800#BLOCKEDISSUE#{blocked_issue_space_id}#{blocked_issue_id}",
        "GSI1PK": "BLOCKEDISSUE#{blocked_issue_space_id}#{blocked_issue_id}",
        "GSI1SK": "500#BLOCKINGISSUE#{blocking_issue_space_id}#{blocking_issue_id}",
    })
    COMPRESSED_ATTRS: ClassVar[set[str]] = set()

    PK: str | None = None
    SK: str | None = None
    GSI1PK: str | None = None
    GSI1SK: str | None = None
    type_version: str = "0.0.1"

    blocking_issue_space_id: str
    blocking_issue_id: str
    blocked_issue_space_id: str
    blocked_issue_id: str
    is_blocking_issue_done: bool
