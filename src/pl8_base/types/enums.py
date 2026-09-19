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
