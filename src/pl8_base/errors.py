# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

class DDBError(Exception):
    """Base class for DDB Issues"""


class DDBInternalError(DDBError):
    """Raised for general service errors"""


class DDBCorruptedError(DDBInternalError):
    """Raised if data read from database is corrupted"""


class DDBMissingError(DDBError):
    """Raised when a requested item does not exist"""


class DDBExistsError(DDBError):
    """Raised when an item already exists"""


class DDBArgsError(DDBError):
    """Raised on invalid arguments passed in"""


class DDBIdCollisionError(DDBError):
    """Raised on ID collision after too many retries"""


class DDBTransactionConflictError(DDBError):
    """Raised on transaction conflict.

    Transient contention. The same request may be retried unchanged; see
    util.retry_on_transaction_conflict.
    """


class DDBVersionConflictError(DDBError):
    """Raised when a write's version condition fails.

    The caller's view of the item is stale, so retrying the same request will
    fail again. The caller must re-read and reapply its change.
    """


class DDBTerminalStatusError(DDBError):
    """Raised when an operation would move an Issue out of DONE, or would
    mutate an Issue whose DONE status forbids the change.

    DONE is terminal; see types.enums.IssueStatus for why that rule is
    load-bearing beyond the product requirement.
    """


class DDBStillBlockedError(DDBError):
    """Raised when an Issue would be transitioned out of BLOCKED while it
    still has active IssueBlockers.

    The caller must delete the remaining IssueBlockers first, or wait for the
    blocking Issues to reach DONE.
    """


class DDBBlockingIssueDoneError(DDBError):
    """Raised when an IssueBlocker would name a DONE Issue as the blocker.

    A DONE Issue blocks nothing; the relationship would be created already
    satisfied.
    """


class DDBSpaceNotEmptyError(DDBError):
    """Raised when deleting a Space whose issue_count is nonzero.

    The caller must delete the Space's Issues first.
    """


class DDBAttachmentStatusError(DDBError):
    """Raised when an IssueAttachment is not in the status the operation needs.

    Every case is the PENDING/UPLOADED transition being one-way; see
    types.enums.AttachmentStatus:
    * confirm_issue_attachment_uploaded found a row that is not PENDING, which
      normally means the upload was already confirmed. The counters moved on
      that first confirm and this call changed nothing, so a caller that is
      only making sure the upload landed may treat this as success; one that
      believes it is confirming a fresh upload has an id that is not the one it
      thinks it is.
    * resign_issue_attachment_upload was asked to re-sign an upload for a row
      that is no longer PENDING. There is nothing left to upload; the caller
      should read the attachment instead.

    Retrying either call unchanged will fail the same way. The status only ever
    moves forward.
    """


class StorageError(Exception):
    """Base class for object storage (S3) issues.

    Deliberately not a DDBError: an attachment is a row plus an object, and
    which half failed is what a caller needs to tell apart. A StorageError
    means the row is fine and the bytes are not.
    """


class StorageInternalError(StorageError):
    """Raised for general object storage errors, and for a manager asked to do
    attachment work it was not configured for.

    Covers an S3 call failing for any reason that is not a missing object, and
    a BasePL8 constructed without an s3_client or a bucket_name reaching one of
    the attachment methods. The first is worth retrying, the second is a
    deployment that needs fixing; the message says which.
    """


class StorageObjectMissingError(StorageError):
    """Raised when an attachment's S3 object does not exist.

    Raised by confirm_issue_attachment_uploaded when the object it was told to
    confirm is not there, which is the ordinary outcome of a caller confirming
    before its upload finished, or of an upload that silently never happened.
    The attachment row is untouched and still PENDING, so the caller may upload
    (re-signing if its URL has expired) and confirm again.
    """


class EventError(Exception):
    """Base class for event issues"""


class EventSendError(EventError):
    """Raised when EventBridge rejects or fails to accept an event.

    Covers both a failed put_events call and a per-entry failure reported
    back with FailedEntryCount > 0.
    """


class EventCorruptedError(EventError):
    """Raised when an event's Detail is missing/unknown type, or malformed"""
