from ..const import RETRY_ISSUE_ID_COLLISIONS
from ..errors import DDBArgsError, DDBIdCollisionError
from ..types import IssueInfo
from ..util import gen_issue_id


class IssueMixin:
    """Issue operations.

    The handle_* methods are not invoked by DynamoDB directly. A separate
    stream handler consumes the DynamoDB stream and publishes an event to
    EventBridge, which routes it to an SQS queue whose consumer calls these.
    None of that plumbing exists yet.

    That path delivers at-least-once and unordered, so every handle_* method
    must be idempotent and safe to apply late. Each gets both by conditioning
    on current item state rather than on a delta, so a duplicated or
    out-of-order event fails its condition and becomes a no-op.
    """

    def create_issue(self, *, space_id, title, description, status):
        """Create an Issue.

        Args:
            space_id (str): id of the issue's space
            title (str): issue title
            description (str): issue description
            status (str): issue status

        Returns: IssueInfo

        Raises:
            DDBIdCollisionError: Collision error on issue_id
            DDBInternalError: internal database error
        """
        for _ in range(RETRY_ISSUE_ID_COLLISIONS):
            issue_id = gen_issue_id()
            issue_info = IssueInfo(
                space_id=space_id,
                issue_id=issue_id,
                title=title,
                description=description,
                status=status,
            )
            # TODO dynamodb PutItem, conditioned. Return on success,
            # retry on issue collision up to RETRY_ISSUE_ID_COLLISIONS tries
            return issue_info

        raise DDBIdCollisionError(f"Issue ID collision after {RETRY_ISSUE_ID_COLLISIONS} tries")

    def get_issue(self, *, space_id, issue_id):
        pk = IssueInfo.KEY_ATTRS["PK"].format(space_id=space_id,
                                              issue_id=issue_id)
        sk = IssueInfo.KEY_ATTRS["SK"]
        return self.get_primary_item(PK=pk, SK=sk)

    def get_issues_by_status(self, *, space_id, status, limit=50, cursor=None):
        # Query GSI1 on GSI1PK=ISSUESPACESTATUS#{space_id}#{status}, sorted by
        # status_updated_at: ScanIndexForward=True gives longest-in-status first
        # Return one page of Issues + continuation cursor
        # cursor uses util methods to encode/decode
        raise NotImplementedError()

    def get_issue_blockers(self, *, space_id, blocked_issue_id, limit=50, cursor=None):
        # Issues that block this Issue: Query GSI1 on
        # GSI1PK=BLOCKEDISSUE#{space_id}#{blocked_issue_id}
        # Return one page of IssueBlockers + continuation cursor
        # No filtering, so a page holds up to limit items and an empty page
        # means the end. Callers wanting only active blockers read
        # IssueInfo.num_active_blockers instead of filtering here.
        # cursor uses util methods to encode/decode
        raise NotImplementedError()

    def get_issue_blocking(self, *, space_id, blocking_issue_id, limit=50, cursor=None):
        # Issues blocked by this Issue: Query the primary table on
        # PK=ISSUE#{space_id}#{blocking_issue_id} with
        # begins_with(SK, "800#BLOCKEDISSUE#")
        # Return one page of IssueBlockers + continuation cursor
        # No filtering, so a page holds up to limit items and an empty page
        # means the end.
        # cursor uses util methods to encode/decode
        raise NotImplementedError()

    def update_issue(self, *, space_id, issue_id, title, description, version=None):
        # TODO
        raise NotImplementedError()

    def transition_issue(self, *, space_id, issue_id, status):
        # TODO
        # Updating status must update GSI1SK and status_updated_at
        # Condition checks:
        # - If transitioning to any status != DONE, Issue.status != DONE
        # - If transitioning to any status != BLOCKED, Issue.num_active_blockers = 0
        raise NotImplementedError()

    def delete_issue(self, *, space_id, issue_id):
        # TODO
        raise NotImplementedError()

    def add_issue_blocker(self, *, blocking_issue_space_id, blocking_issue_id,
                          blocked_issue_space_id, blocked_issue_id):
        """Create an IssueBlocker.

        Raises:
            DDBArgsError: if the blocking and blocked issue are the same Issue
        """
        if (blocking_issue_space_id == blocked_issue_space_id
                and blocking_issue_id == blocked_issue_id):
            raise DDBArgsError("Issue cannot block itself")

        # Blocking cycles between distinct Issues are permitted; the remedy is
        # delete_issue_blocker. See docs architecture/pl8/entities.md.
        # TODO
        # create IssueBlocker and update blocked Issue status to BLOCKED
        # Apply as transaction:
        # - Condition check: Ensure blocking Issue exists and status != DONE
        # Transaction item:
        # - Update blocked Issue status to BLOCKED, increment num_active_blockers
        #   (condition on existence + blocked Issue status != DONE)
        # - Put IssueBlocker (condition on not exists)
        raise NotImplementedError()

    def delete_issue_blocker(self, *, blocking_issue_space_id, blocking_issue_id,
                             blocked_issue_space_id, blocked_issue_id):
        # TODO
        # Remove a blocking relationship. Also the supported way to break a
        # blocking cycle, since neither Issue in a cycle can reach DONE.
        # Apply as transaction, for an IssueBlocker with
        # is_blocking_issue_done=False:
        # - DeleteItem IssueBlocker, condition on existence + is_blocking_issue_done=False
        # - UpdateItem blocked Issue.num_active_blockers -= 1, condition on existence
        #
        # If is_blocking_issue_done=True the counter was already decremented when the
        # blocking Issue went DONE, so DeleteItem alone, condition on existence +
        # is_blocking_issue_done=True.
        #
        # The flag is not known up front and only moves False -> True, so attempt the
        # is_blocking_issue_done=False transaction first and fall back to the bare
        # DeleteItem on condition failure.
        raise NotImplementedError()

    def handle_issue_done(self, *, space_id, issue_id):
        # Triggered by an SQS event; see the IssueMixin docstring for the path
        # TODO
        # Handle Issue transitioned to DONE
        # Query all IssueBlockers blocked by Issue with is_blocking_issue_done=False
        # For each IssueBlocker, apply transaction:
        # - ConditionCheck blocking Issue.status=DONE
        # - UpdateItem IssueBlocker.is_blocking_issue_done=True, condition on existence + is_blocking_issue_done=False
        # - UpdateItem blocked Issue.num_active_blockers -= 1, condition on existence
        raise NotImplementedError()

    def handle_issue_deleted(self, *, space_id, issue_id):
        # Triggered by an SQS event; see the IssueMixin docstring for the path
        # TODO
        # No IssueBlocker may outlive either Issue it names, so sweep both
        # directions. Delivery is at-least-once and unordered, so the conditions
        # below make a duplicated or late event a no-op.
        #
        # Phase 1, Issues this Issue was blocking. These rows live in this Issue's
        # own partition: Query PK=ISSUE#{space_id}#{issue_id},
        # begins_with(SK, "800#BLOCKEDISSUE#")
        # For each IssueBlocker with is_blocking_issue_done=False, apply transaction:
        # - DeleteItem IssueBlocker, condition on existence + is_blocking_issue_done=False
        # - UpdateItem blocked Issue.num_active_blockers -= 1, condition on existence
        #
        # For each IssueBlocker with is_blocking_issue_done=True:
        # DeleteItem IssueBlocker, condition on existence + is_blocking_issue_done=True
        #
        # Phase 2, Issues that were blocking this Issue. These rows live in the
        # blocking Issues' partitions, so sweep them through GSI1:
        # Query GSI1PK=BLOCKEDISSUE#{space_id}#{issue_id}
        # DeleteItem each, condition on existence. No counter update: the Issue
        # holding num_active_blockers is the one being deleted.
        raise NotImplementedError()

    def handle_issue_num_active_blockers_zeroed(self, *, space_id, issue_id):
        # Triggered by an SQS event; see the IssueMixin docstring for the path
        # TODO
        # UpdateItem on Issue: Issue.status=TODO, condition on existence + Issue.num_active_blockers=0 + Issue.status=BLOCKED
        raise NotImplementedError()
