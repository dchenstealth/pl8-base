
from ..const import RETRY_ISSUE_ID_COLLISIONS 
from ..errors import DDBIdCollisionError
from ..types import IssueInfo
from ..util import gen_issue_id


class IssueMixin:
    def create_issue(self, *, space_id, assignee, title,
                     description, status):
        """Create an Issue.

        Does not create Issue

        Args:
            space_id (str): id of the issue's space
            assignee (str): the issue assignee
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
                assignee=assignee,
                title=title,
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
        # Return one page of Issues + continuation cursor
        # cursor uses util methods to encode/decode
        raise NotImplementedError()

    def get_issue_blockers(self, *, space_id, blocked_issue_id, is_blocking_issue_done=None, limit=50, cursor=None):
        # Return one page of IssueBlockers + continuation cursor
        # If is_blocking_issue_done is set, filter
        # cursor uses util methods to encode/decode
        raise NotImplementedError()

    def get_issue_blocking(self, *, space_id, blocked_issue_id, is_blocking_issue_done=None, limit=50, cursor=None):
        # Return one page of IssueBlockers + continuation cursor
        # If is_blocking_issue_done is set, filter
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
        # TODO
        # create IssueBlocker and update blocked Issue status to BLOCKED
        # Apply as transaction:
        # - Condition check: Ensure blocking Issue exists and status != DONE
        # Transaction item:
        # - Update blocked Issue status to BLOCKED, increment num_active_blockers
        #   (condition on existence + blocked Issue status != DONE)
        # - Put IssueBlocker (condition on not exists)
        raise NotImplementedError()

    def handle_issue_done(self, *, space_id, issue_id):
        # Triggered by event sent by DynamoDB stream handler
        # TODO
        # Handle Issue transitioned to DONE
        # Query all IssueBlockers blocked by Issue with is_blocking_issue_done=False
        # For each IssueBlocker, apply transaction:
        # - ConditionCheck blocking Issue.status=DONE
        # - UpdateItem IssueBlocker.is_blocking_issue_done=True, condition on existence + is_blocking_issue_done=False
        # - UpdateItem blocked Issue.num_active_blockers -= 1, condition on existence
        raise NotImplementedError()

    def handle_issue_deleted(self, *, space_id, issue_id):
        # Triggered by event sent by DynamoDB stream handler
        # Query all IssueBlockers blocked by Issue
        # For each IssueBlocker with is_blocking_issue_done=False, apply transaction:
        # - DeleteItem IssueBlocker, condition on existence + is_blocking_issue_done=False
        # - UpdateItem blocked Issue.num_active_blockers -= 1, condition on existence
        #
        # For each IssueBlocker with is_blocking_issue_done=True:
        # DeleteItem IssueBlocker, condition on existence + is_blocking_issue_done=True
        raise NotImplementedError()

    def handle_issue_num_active_blockers_zeroed(self, *, space_id, issue_id):
        # Triggered by event sent by DynamoDB stream handler
        # TODO
        # UpdateItem on Issue: Issue.status=TODO, condition on existence + Issue.num_active_blockers=0 + Issue.status=BLOCKED
        raise NotImplementedError()
