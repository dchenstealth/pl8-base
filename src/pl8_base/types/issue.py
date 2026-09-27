# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

from types import MappingProxyType
from typing import ClassVar
from uuid import uuid7

from ..const import ATTACHMENT_SK_PREFIX
from ..util import isotime, isotime_from_uuid7
from .base import BaseObject
from .enums import AttachmentStatus, IssueStatus


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
    creator: str
    status_updated_at: str | None = None

    # The number of IssueBlockers blocking this Issue with is_blocking_issue_done=False
    # MUST be enforced atomically via transactions.
    num_active_blockers: int = 0

    # The number of IssueComments on this Issue.
    # MUST be enforced atomically via transactions, and MUST NOT move version;
    # see mixins/issue.py issue_num_comments_update.
    num_comments: int = 0

    # The number of UPLOADED IssueAttachments on this Issue, linked or not.
    # PENDING ones are not counted, since their bytes may never arrive.
    # MUST be enforced atomically via transactions, and MUST NOT move version;
    # see mixins/issue.py issue_num_attachments_update.
    num_attachments: int = 0

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


class IssueComment(BaseObject):
    """
    Item representing a note on an Issue.

    Shares the Issue's partition, so an Issue's whole thread is one query and
    no GSI carries it. The 500 prefix sits between the Issue's own 100#INFO row
    and its 800#BLOCKEDISSUE rows, so a bare PK query returns the Issue, then
    its comments oldest first, then its blockers.

    comment_id is a UUIDv7 and created_at is read back out of it, so ordering
    by sort key is ordering by creation timestamp. A UUIDv7 leads with a 48 bit
    big-endian millisecond timestamp and uuid7 counts within each millisecond
    on top of that, so ids sort in the order they were minted. That is what
    keeps the timestamp out of the sort key, and so what lets a comment be
    addressed by its id alone; see util.isotime_from_uuid7.

    An IssueComment MUST NOT outlive its Issue. The Issue's num_comments is
    what holds that, atomically with every comment write, and
    handle_issue_deleted sweeps the rows once the Issue is gone.
    """
    KEY_ATTRS: ClassVar[MappingProxyType] = MappingProxyType({
        "PK": "ISSUE#{space_id}#{issue_id}",
        "SK": "500#COMMENT#{comment_id}",
    })
    COMPRESSED_ATTRS: ClassVar[set[str]] = {"body"}

    PK: str | None = None
    SK: str | None = None
    type_version: str = "0.0.1"

    space_id: str
    issue_id: str
    body: str
    creator: str
    comment_id: str | None = None

    # The number of UPLOADED IssueAttachments linked to this comment. Each is
    # also counted on the Issue.
    # MUST be enforced atomically via transactions, and MUST NOT move version;
    # see mixins/issue.py comment_num_attachments_update.
    num_attachments: int = 0

    def __post_init__(self):
        # comment_id feeds SK, so it must be resolved before
        # BaseObject.__post_init__ renders KEY_ATTRS from self.dict().
        if not self.comment_id:
            self.comment_id = str(uuid7())

        # created_at is read back out of the id rather than taken from a second
        # clock reading, so the two cannot disagree about when the comment was
        # written. Setting it here also makes the base class skip it.
        if not self.created_at:
            self.created_at = isotime_from_uuid7(self.comment_id)

        super().__post_init__()


class IssueAttachment(BaseObject):
    """Item representing a file attached to an Issue, stored in S3.

    Lives in the Issue's partition, like an IssueComment, and like one takes
    its created_at from its UUIDv7 id. GSI1 holds only attachments linked to
    a comment.
    """
    KEY_ATTRS: ClassVar[MappingProxyType] = MappingProxyType({
        "PK": "ISSUE#{space_id}#{issue_id}",
        "SK": ATTACHMENT_SK_PREFIX + "{attachment_id}",
        # Only rendered while comment_id is set; see __post_init__.
        "GSI1PK": "COMMENTATTACHMENT#{space_id}#{issue_id}#{comment_id}",
        "GSI1SK": ATTACHMENT_SK_PREFIX + "{attachment_id}",
    })
    COMPRESSED_ATTRS: ClassVar[set[str]] = set()

    # The S3 key an attachment's bytes live under. Includes space_id because an
    # issue_id is only unique within a Space, and an S3 prefix is the only
    # boundary IAM policies and lifecycle rules can scope to.
    S3_KEY_FORMAT: ClassVar[str] = (
        "space/{space_id}/issue/{issue_id}/attachments/{attachment_id}")

    PK: str | None = None
    SK: str | None = None
    GSI1PK: str | None = None
    GSI1SK: str | None = None
    type_version: str = "0.0.1"

    space_id: str
    issue_id: str
    name: str
    creator: str
    content_type: str
    size: int
    attachment_id: str | None = None

    # The IssueComment this attachment is attributed to, or None. Fixed at
    # creation, since it decides which comment's deletion takes it.
    comment_id: str | None = None

    status: AttachmentStatus = AttachmentStatus.PENDING

    # Unix seconds at which TTL deletes this row, set only while PENDING; see
    # const.ATTACHMENT_PENDING_TTL_SECONDS.
    expires_at: int | None = None

    def __post_init__(self):
        # attachment_id feeds SK, so it must be resolved before
        # BaseObject.__post_init__ renders KEY_ATTRS from self.dict().
        if not self.attachment_id:
            self.attachment_id = str(uuid7())

        # Read out of the id so the two cannot disagree. Setting it here also
        # makes the base class skip it.
        if not self.created_at:
            self.created_at = isotime_from_uuid7(self.attachment_id)

        super().__post_init__()

        # super() rendered GSI1 keys with a literal "None" for an unlinked
        # attachment. Clear them so serialize() omits them and the row stays
        # out of the index.
        if self.comment_id is None:
            self.GSI1PK = None
            self.GSI1SK = None

    @property
    def s3_key(self):
        """The S3 key this attachment's bytes live under."""
        return self.S3_KEY_FORMAT.format(space_id=self.space_id,
                                         issue_id=self.issue_id,
                                         attachment_id=self.attachment_id)

    @property
    def is_uploaded(self):
        return self.status == AttachmentStatus.UPLOADED


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
