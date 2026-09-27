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

    From confirm or re-sign on an attachment that is already UPLOADED. A caller
    confirming only to make sure its upload landed may treat this as success.
    Retrying will fail the same way.
    """


class StorageError(Exception):
    """Base class for object storage (S3) issues.

    Not a DDBError, so a caller can tell a failed object from a failed row.
    """


class StorageInternalError(StorageError):
    """Raised for general object storage errors, and for a manager asked to do
    attachment work without an s3_client or bucket_name.
    """


class StorageObjectMissingError(StorageError):
    """Raised when an attachment's S3 object does not exist.

    Usually a confirm that arrived before the upload finished. The row is still
    PENDING, so the caller may upload and confirm again.
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
