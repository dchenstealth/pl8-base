# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

from enum import StrEnum


class IssueStatus(StrEnum):
    """Issue lifecycle states.

    DONE is terminal: an Issue MUST NOT be transitioned out of DONE. That rule
    is load-bearing beyond the product requirement, because it means
    IssueBlocker.is_blocking_issue_done only ever moves False -> True:
    * handle_issue_done relies on that monotonicity to decrement
      num_active_blockers exactly once, since event delivery is
      at-least-once and a replay re-fails the False condition.
    * add_issue_blocker relies on it to check the blocking Issue's status
      only once, at creation; a blocking Issue cannot later stop being DONE.

    Reopening an Issue would break both and would require reworking how
    num_active_blockers is maintained.
    """

    TODO = "TODO"
    BLOCKED = "BLOCKED"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"


class AttachmentStatus(StrEnum):
    """IssueAttachment upload states.

    An attachment row is written before its bytes exist: the row is what the
    presigned POST is signed against, so PENDING means "an upload has been
    authorized for this object", not "a file is attached".

    UPLOADED is terminal, and the transition is one-way: an attachment MUST NOT
    go back to PENDING. That rule is load-bearing beyond tidiness, because the
    status is the condition the counters hang off:
    * confirm_issue_attachment_uploaded conditions its write on
      status = PENDING and increments IssueInfo.num_attachments, and the
      IssueComment's when the attachment is linked, in the same transaction.
      Event and API delivery are at-least-once, so a replayed confirm
      re-fails that condition, the whole transaction becomes a no-op, and the
      increment therefore happens exactly once.
    * delete_issue_attachment conditions its decrement on status = UPLOADED for
      the same reason in reverse: a PENDING attachment was never counted, so
      decrementing for one would drive the counter negative.

    Re-signing an upload (resign_issue_attachment_upload) deliberately does not
    move the status: it hands out a fresh URL for a row that is still PENDING,
    which is exactly the state that lets the eventual confirm count once.
    """

    PENDING = "PENDING"
    UPLOADED = "UPLOADED"
