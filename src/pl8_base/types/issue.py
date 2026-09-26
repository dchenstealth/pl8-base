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

    # The number of UPLOADED IssueAttachments on this Issue, linked to one of
    # its comments or not. A PENDING attachment is deliberately not counted:
    # its bytes may never arrive, and its row may be reaped by TTL.
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

    # The number of UPLOADED IssueAttachments linked to this comment. The same
    # attachment is also counted on the Issue, so the two counters overlap by
    # design: an attachment belongs to the Issue and is optionally attributed
    # to one of its comments.
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

    The row and the object are two halves of one attachment, and the row comes
    first: it is what the presigned POST is signed against, so it exists while
    the bytes are still in flight. status is which half is true yet; see
    types/enums.py AttachmentStatus.

    Shares the Issue's partition, like an IssueComment, so an Issue's whole
    attachment list is one query and no GSI carries it. The 600 prefix sits
    between the comments' 500 and the blockers' 800, so a bare PK query returns
    the Issue, then its comments, then its attachments, then its blockers.

    attachment_id is a UUIDv7 and created_at is read back out of it, exactly as
    IssueComment's is and for the same reasons: ordering by sort key is
    ordering by creation time, and the attachment stays addressable by its id
    alone. See util.isotime_from_uuid7.

    An attachment MAY be linked to one of the Issue's IssueComments, which is
    what comment_id records. GSI1 is that link, so the index is sparse: an
    unlinked attachment has no GSI1 keys at all and is simply not in it. The
    keys are rendered only when comment_id is set (see __post_init__) and
    BaseObject.serialize omits a key attr that is None, which is what makes
    that expressible in a row DynamoDB will accept.

    An IssueAttachment MUST NOT outlive its Issue, and a linked one MUST NOT
    outlive its IssueComment. The Issue's num_attachments and, when linked, the
    comment's hold the first half atomically with every counted write, and the
    handle_* sweeps remove the rows once the parent is gone. The S3 object is
    removed by handle_issue_attachment_deleted, driven off this row's delete,
    so the object never outlives the row either.
    """
    # The sort key prefix is composed from const rather than spelled out,
    # unlike IssueComment's literal "500#COMMENT#", because this one is also a
    # query bound: AttachmentMixin ranges over it to read an Issue's
    # attachments out of the partition the Issue shares with its other rows.
    KEY_ATTRS: ClassVar[MappingProxyType] = MappingProxyType({
        "PK": "ISSUE#{space_id}#{issue_id}",
        "SK": ATTACHMENT_SK_PREFIX + "{attachment_id}",
        # Only rendered while comment_id is set; see __post_init__.
        "GSI1PK": "COMMENTATTACHMENT#{space_id}#{issue_id}#{comment_id}",
        "GSI1SK": ATTACHMENT_SK_PREFIX + "{attachment_id}",
    })
    COMPRESSED_ATTRS: ClassVar[set[str]] = set()

    # The S3 key an attachment's bytes live under. A format string on the class,
    # not a stored field, for the same reason PK and SK are: the layout lives in
    # exactly one place, and a row can never carry a key that disagrees with it.
    #
    # space_id is in the key even though attachment_id alone is unique. An
    # issue_id is only unique within its Space, so a key without the Space
    # would put two Spaces' Issues under one prefix, and everything that acts
    # on an S3 prefix would then span Spaces: an IAM policy scoped to a
    # Space's prefix, a lifecycle rule, or a prefix delete cleaning up one
    # Space. The Space boundary has to be above the Issue in the key for any of
    # those to be expressible.
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

    # The IssueComment this attachment is attributed to, or None for one
    # attached to the Issue itself. Fixed at creation: moving an attachment
    # between comments would rewrite its GSI1 keys, and nothing here does that.
    comment_id: str | None = None

    status: AttachmentStatus = AttachmentStatus.PENDING

    # Unix seconds DynamoDB's TTL reaps this row at, set only while the row is
    # PENDING and removed by confirm; see const.ATTACHMENT_PENDING_TTL_SECONDS.
    expires_at: int | None = None

    def __post_init__(self):
        # attachment_id feeds SK, so it must be resolved before
        # BaseObject.__post_init__ renders KEY_ATTRS from self.dict().
        if not self.attachment_id:
            self.attachment_id = str(uuid7())

        # created_at is read back out of the id rather than taken from a second
        # clock reading, so the two cannot disagree about when the attachment
        # was initiated. Setting it here also makes the base class skip it.
        if not self.created_at:
            self.created_at = isotime_from_uuid7(self.attachment_id)

        super().__post_init__()

        # GSI1 is the comment link, so an unlinked attachment must not be in
        # the index at all. BaseObject.__post_init__ renders every KEY_ATTRS
        # entry that is still None from one self.dict() snapshot, and it has no
        # notion of a key that only sometimes applies, so with comment_id=None
        # it has just produced the literal
        # "COMMENTATTACHMENT#{space_id}#{issue_id}#None": every unlinked
        # attachment in the table filed under one garbage index partition,
        # returned by a get_issue_comment_attachments call for a comment whose
        # id is the string "None". Take the render back rather than let that
        # reach a row; serialize() then omits the attributes entirely, which is
        # what makes the index sparse.
        #
        # Done after super() rather than before because there is no way to ask
        # that pass to skip an entry: it renders whatever is None, so a value
        # put here to fence it off would be the value that got stored.
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
