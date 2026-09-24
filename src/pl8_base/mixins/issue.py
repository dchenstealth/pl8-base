# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

from botocore.exceptions import ClientError

from ..const import (
    GSI1_INDEX_NAME,
    RETRY_ISSUE_ID_COLLISIONS,
)
from ..errors import (
    DDBArgsError,
    DDBBlockingIssueDoneError,
    DDBExistsError,
    DDBIdCollisionError,
    DDBInternalError,
    DDBMissingError,
    DDBStillBlockedError,
    DDBTerminalStatusError,
)
from ..types import IssueBlocker, IssueInfo, IssueStatus
from ..util import (
    gen_issue_id,
    isotime,
    retry_on_transaction_conflict,
    validate_creator,
    validate_issue_status,
    validate_space_id,
)

BLOCKER_SK_PREFIX = "800#BLOCKEDISSUE#"


class IssueMixin:
    """Issue operations.

    The handle_* methods are not invoked by DynamoDB directly. pl8-services'
    pl8-stream-handler consumes the DynamoDB stream and publishes an event to
    EventBridge, which routes it to an SQS queue whose consumer,
    pl8-event-handler, calls these.

    That path delivers at-least-once and unordered, so every handle_* method
    must be idempotent and safe to apply late. Each gets both by conditioning
    on current item state rather than on a delta, so a duplicated or
    out-of-order event fails its condition and becomes a no-op.

    Every public method validates its space_id before it reaches a key, the
    same as SpaceMixin does. An Issue key composes ISSUE#{space_id}#{issue_id},
    so a space_id carrying a "#" would alias one Issue onto another
    (space_id, issue_id) pair; see util.validate_space_id.

    create_issue and delete_issue also keep the Space's issue_count in step,
    in the same transaction as the Issue write. That is what refuses an Issue
    in a missing Space and what lets delete_space refuse a Space that still
    has Issues; see SpaceMixin.

    An Issue holds num_comments over its own IssueComments the same way, but
    the rule it enforces is the opposite one: a Space refuses to be deleted
    while it holds Issues, whereas an Issue is deleted whatever its
    num_comments and handle_issue_deleted sweeps the comments after it. See
    CommentMixin.
    """

    # ------------------------------------------------------------------
    # Key and condition helpers
    # ------------------------------------------------------------------

    def issue_pk(self, space_id, issue_id):
        return IssueInfo.KEY_ATTRS["PK"].format(space_id=space_id,
                                                issue_id=issue_id)

    def issue_info_key(self, space_id, issue_id):
        return {
            "PK": self.ts.serialize(self.issue_pk(space_id, issue_id)),
            "SK": self.ts.serialize(IssueInfo.KEY_ATTRS["SK"]),
        }

    def issue_blocker_key(self, issue_blocker):
        """The serialized primary key of an IssueBlocker row."""
        return {
            "PK": self.ts.serialize(issue_blocker.PK),
            "SK": self.ts.serialize(issue_blocker.SK),
        }

    def issue_num_comments_update(self, space_id, issue_id, delta):
        """Update dict adding delta to an Issue's num_comments.

        For a transaction alongside the comment write it counts. The condition
        on the Issue existing is also what refuses a comment whose Issue is
        missing, and so what keeps a comment from being written into a
        partition no Issue owns.

        Built by hand rather than with _build_update, which bumps version and
        updated_at, for the same reason space_issue_count_update is: an Issue's
        version fences update_issue and transition_issue, and commenting is not
        an edit to the Issue itself, so comment traffic must not fail a
        concurrent version-fenced write with a spurious
        DDBVersionConflictError.
        """
        return {
            "TableName": self.table_name,
            "Key": self.issue_info_key(space_id, issue_id),
            "UpdateExpression": "ADD #num_comments :delta",
            "ConditionExpression": "attribute_exists(#PK)",
            "ExpressionAttributeNames": {
                "#PK": "PK",
                "#num_comments": "num_comments",
            },
            "ExpressionAttributeValues": {
                ":delta": self.serialize_value(delta),
            },
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }

    def status_attrs(self, *, space_id, issue_id, status):
        """The attrs a status change must write together.

        status_updated_at feeds GSI1SK and status feeds GSI1PK, so a write that
        moved status without these would leave the Issue indexed under its old
        status. Always set as one group.
        """
        status_updated_at = isotime()
        return {
            "status": status,
            "status_updated_at": status_updated_at,
            "GSI1PK": IssueInfo.KEY_ATTRS["GSI1PK"].format(space_id=space_id,
                                                           status=status),
            "GSI1SK": IssueInfo.KEY_ATTRS["GSI1SK"].format(
                status_updated_at=status_updated_at, issue_id=issue_id),
        }

    def update_issue_item(self, update, *, space_id, issue_id, version=None,
                          classify=None):
        """Apply a built update to an Issue and return it as written.

        Args:
            update (dict): an update dict from _build_update
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            version (int or None): version the write was conditioned on
            classify (callable or None): called with the pre-write IssueInfo
                when a domain condition failed, to raise the matching error

        Returns:
            IssueInfo: the Issue after the write

        Raises:
            DDBMissingError: if the Issue does not exist
            DDBVersionConflictError: if version is set and did not match
            DDBTransactionConflictError: if a transaction held the Issue
            DDBInternalError: internal database error
        """
        return self.apply_update(
            update,
            entity="Issue",
            ref=f"{space_id}#{issue_id}",
            version=version,
            classify=classify,
            log_context={"space_id": space_id, "issue_id": issue_id},
        )

    # ------------------------------------------------------------------
    # Issue CRUD
    # ------------------------------------------------------------------

    @retry_on_transaction_conflict()
    def create_issue(self, *, space_id, title, description, status, creator):
        """Create an Issue and count it against its Space.

        The Space row takes a write for every Issue created in it, so
        concurrent creates in one Space contend on it; a conflict is retried.

        Args:
            space_id (str): id of the issue's space
            title (str): issue title
            description (str): issue description
            status (str or IssueStatus): issue status
            creator (str): who or what is creating the Issue, recorded as
                supplied and never verified; see util.validate_creator

        Returns: IssueInfo

        Raises:
            DDBArgsError: if space_id, status or creator is invalid, or
                description is not a string
            DDBMissingError: if the Space does not exist
            DDBIdCollisionError: Collision error on issue_id
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)
        validate_creator(creator)
        status = validate_issue_status(status)

        for _ in range(RETRY_ISSUE_ID_COLLISIONS):
            issue_id = gen_issue_id()
            issue_info = IssueInfo(
                space_id=space_id,
                issue_id=issue_id,
                title=title,
                description=description,
                status=status,
                creator=creator,
            )

            # Ordering is load-bearing: CancellationReasons come back
            # positionally.
            items = [
                {"Update": self.space_issue_count_update(space_id, 1)},
                {"Put": {
                    "TableName": self.table_name,
                    "Item": issue_info.serialize(ts=self.ts),
                    "ConditionExpression": "attribute_not_exists(#PK)",
                    "ExpressionAttributeNames": {"#PK": "PK"},
                }},
            ]

            try:
                self.dynamodb_client.transact_write_items(TransactItems=items)
            except ClientError as exc:
                self.raise_for_transaction_conflict(exc)

                failed, _ = self.failed_reason_item(exc, 0)
                if failed:
                    raise DDBMissingError(
                        f"Space not found: {space_id}") from exc

                failed, _ = self.failed_reason_item(exc, 1)
                if failed:
                    # The id is taken; generate another rather than overwrite
                    continue

                self.log_client_error(exc)
                raise DDBInternalError(
                    f"Error creating issue: {exc!s}") from exc

            return issue_info

        raise DDBIdCollisionError(f"Issue ID collision after {RETRY_ISSUE_ID_COLLISIONS} tries")

    def get_issue(self, *, space_id, issue_id):
        """Load one Issue.

        Raises:
            DDBArgsError: if space_id is invalid
            DDBMissingError: if the Issue does not exist
            DDBCorruptedError: if the item cannot be parsed
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        return self.get_primary_item(PK=self.issue_pk(space_id, issue_id),
                                     SK=IssueInfo.KEY_ATTRS["SK"])

    def get_issues_by_status(self, *, space_id, status, limit=50, cursor=None):
        """One page of Issues in a status, longest-in-status first.

        Sorted by status_updated_at ascending, so the head of the first page is
        whatever has sat in this status the longest.

        Returns:
            tuple: (list[IssueInfo], str or None)

        Raises:
            DDBArgsError: if space_id, status or cursor is invalid
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)
        status = validate_issue_status(status)

        gsi1pk = IssueInfo.KEY_ATTRS["GSI1PK"].format(space_id=space_id,
                                                      status=status)
        return self.run_query({
            "IndexName": GSI1_INDEX_NAME,
            "KeyConditionExpression": "#gsi1pk = :gsi1pk",
            "ExpressionAttributeNames": {"#gsi1pk": "GSI1PK"},
            "ExpressionAttributeValues": {":gsi1pk": self.ts.serialize(gsi1pk)},
            "ScanIndexForward": True,
        }, cursor=cursor, limit=limit)

    def get_issue_blockers(self, *, space_id, blocked_issue_id, limit=50,
                           cursor=None):
        """One page of the IssueBlockers blocking this Issue.

        These rows live in the blocking Issues' partitions, so they are only
        reachable through GSI1.

        No filtering, so a page holds up to limit items and an empty page means
        the end. Callers wanting only active blockers read
        IssueInfo.num_active_blockers instead of filtering here.

        Returns:
            tuple: (list[IssueBlocker], str or None)

        Raises:
            DDBArgsError: if space_id or cursor is invalid
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        gsi1pk = IssueBlocker.KEY_ATTRS["GSI1PK"].format(
            blocked_issue_space_id=space_id,
            blocked_issue_id=blocked_issue_id)
        return self.run_query({
            "IndexName": GSI1_INDEX_NAME,
            "KeyConditionExpression": "#gsi1pk = :gsi1pk",
            "ExpressionAttributeNames": {"#gsi1pk": "GSI1PK"},
            "ExpressionAttributeValues": {":gsi1pk": self.ts.serialize(gsi1pk)},
        }, cursor=cursor, limit=limit)

    def get_issue_blocking(self, *, space_id, blocking_issue_id, limit=50,
                           cursor=None):
        """One page of the IssueBlockers this Issue holds over others.

        These rows share the blocking Issue's partition, so the SK prefix is
        what keeps its own INFO row out of the result. That is a key condition
        rather than a filter, so Limit counts only blocker rows.

        Returns:
            tuple: (list[IssueBlocker], str or None)

        Raises:
            DDBArgsError: if space_id or cursor is invalid
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        pk = IssueBlocker.KEY_ATTRS["PK"].format(
            blocking_issue_space_id=space_id,
            blocking_issue_id=blocking_issue_id)
        return self.run_query({
            "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :sk)",
            "ExpressionAttributeNames": {"#pk": "PK", "#sk": "SK"},
            "ExpressionAttributeValues": {
                ":pk": self.ts.serialize(pk),
                ":sk": self.ts.serialize(BLOCKER_SK_PREFIX),
            },
        }, cursor=cursor, limit=limit)

    def get_issue_partition(self, *, space_id, issue_id, limit=None,
                            cursor=None):
        """One page of every row in an Issue's partition, read consistently.

        For the delete sweep, which must see every row the Issue owns: its
        comments, its outbound IssueBlockers, and the info row itself if it is
        somehow still there. Reading them as one query rather than as a query
        per row type is what makes the sweep a single view of the partition
        instead of several taken at different moments.

        ConsistentRead, unlike every other query here. The sweep runs once and
        nothing retries it, so a row missing from an eventually-consistent page
        is a row that outlives its Issue for good. The set is closed by then,
        since neither a comment nor a blocker can be written against an Issue
        whose info row is gone, so a consistent read is guaranteed to be
        complete rather than merely likely to be.

        Returns:
            tuple: (list[BaseObject], str or None)

        Raises:
            DDBArgsError: if space_id or cursor is invalid
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        return self.run_query({
            "KeyConditionExpression": "#pk = :pk",
            "ExpressionAttributeNames": {"#pk": "PK"},
            "ExpressionAttributeValues": {
                ":pk": self.ts.serialize(self.issue_pk(space_id, issue_id)),
            },
            "ConsistentRead": True,
        }, cursor=cursor, limit=limit)

    @retry_on_transaction_conflict()
    def update_issue(self, *, space_id, issue_id, title, description,
                     version=None):
        """Update an Issue's title and description.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            title (str): new issue title
            description (str): new issue description
            version (int or None): if set, fence the write on this version

        Returns: IssueInfo

        Raises:
            DDBArgsError: if space_id is invalid, or description is not a
                string
            DDBMissingError: if the Issue does not exist
            DDBVersionConflictError: if version is set and did not match
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        update = self._build_update(
            PK=self.issue_pk(space_id, issue_id),
            SK=IssueInfo.KEY_ATTRS["SK"],
            version=version,
            title=title,
            description=IssueInfo.compress_value("description", description),
        )
        return self.update_issue_item(update, space_id=space_id,
                                      issue_id=issue_id, version=version)

    @retry_on_transaction_conflict()
    def transition_issue(self, *, space_id, issue_id, status, version=None):
        """Move an Issue to a new status.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            status (str or IssueStatus): status to move to
            version (int or None): if set, fence the write on this version

        Returns: IssueInfo

        Raises:
            DDBArgsError: if space_id or status is invalid
            DDBMissingError: if the Issue does not exist
            DDBVersionConflictError: if version is set and did not match
            DDBTerminalStatusError: if the Issue is DONE and would leave it
            DDBStillBlockedError: if the Issue still has active blockers
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)
        status = validate_issue_status(status)

        # Moving to DONE from DONE is not a transition out of DONE, so the
        # terminal rule only applies to the other targets.
        excluded_vals = ({} if status == IssueStatus.DONE
                         else {"status": IssueStatus.DONE})
        # Becoming BLOCKED is always allowed; leaving it is not while blockers
        # remain.
        expected_vals = ({} if status == IssueStatus.BLOCKED
                         else {"num_active_blockers": 0})

        update = self._build_update(
            PK=self.issue_pk(space_id, issue_id),
            SK=IssueInfo.KEY_ATTRS["SK"],
            version=version,
            expected_vals=expected_vals,
            excluded_vals=excluded_vals,
            **self.status_attrs(space_id=space_id, issue_id=issue_id,
                                status=status),
        )

        def classify(old):
            if status != IssueStatus.DONE and old.is_done:
                raise DDBTerminalStatusError(
                    f"Issue {space_id}#{issue_id} is DONE")

            if status != IssueStatus.BLOCKED and old.num_active_blockers:
                raise DDBStillBlockedError(
                    f"Issue {space_id}#{issue_id} has "
                    f"{old.num_active_blockers} active blockers")

        return self.update_issue_item(update, space_id=space_id,
                                      issue_id=issue_id, version=version,
                                      classify=classify)

    @retry_on_transaction_conflict()
    def delete_issue(self, *, space_id, issue_id):
        """Delete an Issue's info row and uncount it from its Space.

        The IssueBlockers naming it are swept by handle_issue_deleted, which
        the stream handler drives once this write lands.

        Raises:
            DDBArgsError: if space_id is invalid
            DDBMissingError: if the Issue does not exist
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        # Ordering is load-bearing: CancellationReasons come back positionally.
        items = [
            {"Delete": {
                "TableName": self.table_name,
                "Key": self.issue_info_key(space_id, issue_id),
                "ConditionExpression": "attribute_exists(#PK)",
                "ExpressionAttributeNames": {"#PK": "PK"},
            }},
            {"Update": self.space_issue_count_update(space_id, -1)},
        ]

        try:
            self.dynamodb_client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            self.raise_for_transaction_conflict(exc)

            failed, _ = self.failed_reason_item(exc, 0)
            if failed:
                raise DDBMissingError(
                    f"Issue not found: {space_id}#{issue_id}") from exc

            # Unreachable while the invariant holds: a counted Issue keeps its
            # Space from being deleted.
            failed, _ = self.failed_reason_item(exc, 1)
            if failed:
                raise DDBMissingError(f"Space not found: {space_id}") from exc

            self.log_client_error(exc)
            raise DDBInternalError(f"Error deleting issue: {exc!s}") from exc

    # ------------------------------------------------------------------
    # Issue blockers
    # ------------------------------------------------------------------

    @retry_on_transaction_conflict()
    def add_issue_blocker(self, *, blocking_issue_space_id, blocking_issue_id,
                          blocked_issue_space_id, blocked_issue_id):
        """Create an IssueBlocker.

        Applied as one transaction so the IssueBlocker row and the blocked
        Issue's counter can never disagree.

        Returns: IssueBlocker

        Raises:
            DDBArgsError: if either space_id is invalid, or the blocking and
                blocked issue are the same Issue
            DDBMissingError: if either Issue does not exist
            DDBBlockingIssueDoneError: if the blocking Issue is DONE
            DDBTerminalStatusError: if the blocked Issue is DONE
            DDBExistsError: if the IssueBlocker already exists
            DDBTransactionConflictError: if every attempt conflicts
        """
        validate_space_id(blocking_issue_space_id)
        validate_space_id(blocked_issue_space_id)

        if (blocking_issue_space_id == blocked_issue_space_id
                and blocking_issue_id == blocked_issue_id):
            raise DDBArgsError("Issue cannot block itself")

        # Blocking cycles between distinct Issues are permitted; the remedy is
        # delete_issue_blocker. See pl8-docs architecture/entities.md.
        issue_blocker = IssueBlocker(
            blocking_issue_space_id=blocking_issue_space_id,
            blocking_issue_id=blocking_issue_id,
            blocked_issue_space_id=blocked_issue_space_id,
            blocked_issue_id=blocked_issue_id,
            is_blocking_issue_done=False,
        )

        # Ordering is load-bearing: CancellationReasons come back positionally.
        items = [
            {"ConditionCheck": {
                "TableName": self.table_name,
                "Key": self.issue_info_key(blocking_issue_space_id,
                                           blocking_issue_id),
                "ConditionExpression": "attribute_exists(#PK) AND #status <> :done",
                "ExpressionAttributeNames": {"#PK": "PK", "#status": "status"},
                "ExpressionAttributeValues": {
                    ":done": self.serialize_value(IssueStatus.DONE),
                },
                "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
            }},
            {"Update": self._build_update(
                PK=self.issue_pk(blocked_issue_space_id, blocked_issue_id),
                SK=IssueInfo.KEY_ATTRS["SK"],
                excluded_vals={"status": IssueStatus.DONE},
                increments={"num_active_blockers": 1},
                **self.status_attrs(space_id=blocked_issue_space_id,
                                    issue_id=blocked_issue_id,
                                    status=IssueStatus.BLOCKED),
            )},
            {"Put": {
                "TableName": self.table_name,
                "Item": issue_blocker.serialize(ts=self.ts),
                "ConditionExpression": "attribute_not_exists(#PK)",
                "ExpressionAttributeNames": {"#PK": "PK"},
            }},
        ]

        try:
            self.dynamodb_client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            self.raise_for_transaction_conflict(exc)

            failed, old = self.failed_reason_item(exc, 0)
            if failed:
                if old is None:
                    raise DDBMissingError(
                        "Blocking issue not found: "
                        f"{blocking_issue_space_id}#{blocking_issue_id}") from exc
                raise DDBBlockingIssueDoneError(
                    "Blocking issue is DONE: "
                    f"{blocking_issue_space_id}#{blocking_issue_id}") from exc

            failed, old = self.failed_reason_item(exc, 1)
            if failed:
                if old is None:
                    raise DDBMissingError(
                        "Blocked issue not found: "
                        f"{blocked_issue_space_id}#{blocked_issue_id}") from exc
                raise DDBTerminalStatusError(
                    "Blocked issue is DONE: "
                    f"{blocked_issue_space_id}#{blocked_issue_id}") from exc

            failed, _ = self.failed_reason_item(exc, 2)
            if failed:
                raise DDBExistsError("IssueBlocker exists") from exc

            self.log_client_error(exc)
            raise DDBInternalError(
                f"Error adding issue blocker: {exc!s}") from exc

        return issue_blocker

    @retry_on_transaction_conflict()
    def delete_issue_blocker(self, *, blocking_issue_space_id,
                             blocking_issue_id, blocked_issue_space_id,
                             blocked_issue_id):
        """Remove a blocking relationship.

        Also the supported way to break a blocking cycle, since neither Issue
        in a cycle can reach DONE.

        The counter only needs decrementing for a blocker that is still active.
        is_blocking_issue_done is not known up front and only ever moves
        False -> True, so attempt the active form first and fall back to a bare
        delete when its condition fails.

        Raises:
            DDBArgsError: if either space_id is invalid
            DDBMissingError: if the IssueBlocker does not exist
            DDBTransactionConflictError: if every attempt conflicts
        """
        validate_space_id(blocking_issue_space_id)
        validate_space_id(blocked_issue_space_id)

        # Only the key matters here; is_blocking_issue_done is what the write
        # conditions on rather than what it carries.
        target = IssueBlocker(
            blocking_issue_space_id=blocking_issue_space_id,
            blocking_issue_id=blocking_issue_id,
            blocked_issue_space_id=blocked_issue_space_id,
            blocked_issue_id=blocked_issue_id,
            is_blocking_issue_done=False,
        )
        blocker_key = self.issue_blocker_key(target)

        try:
            self.dynamodb_client.transact_write_items(TransactItems=[
                {"Delete": self.active_blocker_delete(blocker_key)},
                {"Update": self._build_update(
                    PK=self.issue_pk(blocked_issue_space_id,
                                     blocked_issue_id),
                    SK=IssueInfo.KEY_ATTRS["SK"],
                    increments={"num_active_blockers": -1},
                )},
            ])
            return
        except ClientError as exc:
            self.raise_for_transaction_conflict(exc)

            failed, _ = self.failed_reason_item(exc, 1)
            if failed:
                raise DDBMissingError(
                    "Blocked issue not found: "
                    f"{blocked_issue_space_id}#{blocked_issue_id}") from exc

            failed, _ = self.failed_reason_item(exc, 0)
            if not failed:
                self.log_client_error(exc)
                raise DDBInternalError(
                    f"Error deleting issue blocker: {exc!s}") from exc

        # The blocker is either already satisfied, in which case the counter
        # was decremented when the blocking Issue went DONE, or it is gone.
        try:
            self.dynamodb_client.delete_item(
                TableName=self.table_name,
                Key=blocker_key,
                ConditionExpression="attribute_exists(#PK) AND #done = :true",
                ExpressionAttributeNames={
                    "#PK": "PK",
                    "#done": "is_blocking_issue_done",
                },
                ExpressionAttributeValues={":true": self.serialize_value(True)},
            )
        except ClientError as exc:
            self.raise_for_transaction_conflict(exc)

            if self.is_condition_failure(exc):
                raise DDBMissingError("IssueBlocker not found") from exc

            self.log_client_error(exc)
            raise DDBInternalError(
                f"Error deleting issue blocker: {exc!s}") from exc

    def active_blocker_delete(self, blocker_key):
        """Delete dict for an IssueBlocker that must still be active."""
        return {
            "TableName": self.table_name,
            "Key": blocker_key,
            "ConditionExpression": "attribute_exists(#PK) AND #done = :false",
            "ExpressionAttributeNames": {
                "#PK": "PK",
                "#done": "is_blocking_issue_done",
            },
            "ExpressionAttributeValues": {":false": self.serialize_value(False)},
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def handle_issue_done(self, *, space_id, issue_id):
        """Handle an Issue having been transitioned to DONE.

        Triggered by an SQS event; see the IssueMixin docstring for the path.

        Marks every IssueBlocker this Issue holds as satisfied and drops the
        blocked Issues' counters. Each write conditions on the state it
        expects, so a replayed or late event is a no-op.

        Raises:
            DDBArgsError: if space_id is invalid
        """
        validate_space_id(space_id)

        for issue_blocker in self.paginate(self.get_issue_blocking,
                                           space_id=space_id,
                                           blocking_issue_id=issue_id):
            if issue_blocker.is_blocking_issue_done:
                continue

            self.satisfy_issue_blocker(issue_blocker, space_id=space_id,
                                       issue_id=issue_id)

    def satisfy_issue_blocker(self, issue_blocker, *, space_id, issue_id):
        """Mark one IssueBlocker satisfied and decrement the blocked counter.

        DONE is terminal, so is_blocking_issue_done only ever moves
        False -> True. That is what makes the decrement happen exactly once: a
        replay re-fails the is_blocking_issue_done=False condition and the
        whole transaction becomes a no-op.
        """
        items = [
            {"ConditionCheck": {
                "TableName": self.table_name,
                "Key": self.issue_info_key(space_id, issue_id),
                "ConditionExpression": "attribute_exists(#PK) AND #status = :done",
                "ExpressionAttributeNames": {"#PK": "PK", "#status": "status"},
                "ExpressionAttributeValues": {
                    ":done": self.serialize_value(IssueStatus.DONE),
                },
            }},
            {"Update": self._build_update(
                PK=issue_blocker.PK,
                SK=issue_blocker.SK,
                expected_vals={"is_blocking_issue_done": False},
                is_blocking_issue_done=True,
            )},
            {"Update": self._build_update(
                PK=self.issue_pk(issue_blocker.blocked_issue_space_id,
                                 issue_blocker.blocked_issue_id),
                SK=IssueInfo.KEY_ATTRS["SK"],
                increments={"num_active_blockers": -1},
            )},
        ]

        self.apply_idempotent_transaction(
            items, "Error satisfying issue blocker")

    def handle_issue_deleted(self, *, space_id, issue_id):
        """Handle an Issue having been deleted.

        Triggered by an SQS event; see the IssueMixin docstring for the path.

        Nothing the Issue owned may outlive it: no IssueComment, and no
        IssueBlocker naming it in either direction. Delivery is at-least-once
        and unordered, so every write conditions on the state it expects and a
        duplicate event is a no-op.

        Raises:
            DDBArgsError: if space_id is invalid
        """
        validate_space_id(space_id)

        # Phase 1, everything in this Issue's own partition: its IssueComments
        # and the IssueBlockers it held over other Issues. One consistent
        # query, so the sweep acts on a single complete view of the partition
        # rather than on a page per row type; see get_issue_partition.
        for item in self.paginate(self.get_issue_partition, space_id=space_id,
                                  issue_id=issue_id):
            if isinstance(item, IssueInfo):
                # The info row is deleted before this event is sent, so an
                # Issue standing here is a different Issue that has taken the
                # same id, and the rows around it are its own. Sweeping them
                # would delete live data, and deleting the info row itself
                # would strand its Space's issue_count. Leave the partition
                # alone; the deleted Issue's own rows went with the id.
                self.logger.warning(
                    "Skipping sweep of a live Issue partition",
                    space_id=space_id, issue_id=issue_id)
                return

            if isinstance(item, IssueBlocker):
                self.delete_blocker_for_sweep(item)
            else:
                # An IssueComment holds no counter of its own, and the Issue
                # that counted it is already gone.
                self.delete_row(item)

        # Phase 2, Issues that were blocking this Issue. These rows live in the
        # blocking Issues' partitions, so they are only reachable via GSI1 and
        # cannot be read consistently: a blocker added moments before the
        # delete may not be in the index yet, and nothing retries this sweep.
        # No counter update either way: the Issue holding num_active_blockers
        # is the one being deleted.
        for issue_blocker in self.paginate(self.get_issue_blockers,
                                           space_id=space_id,
                                           blocked_issue_id=issue_id):
            self.delete_row(issue_blocker)

    def delete_blocker_for_sweep(self, issue_blocker):
        """Delete one outbound IssueBlocker, decrementing if it was active.

        is_blocking_issue_done comes from the sweep's query, so it may already
        be stale: handle_issue_done can flip it False -> True between that read
        and this write. The row must go either way, so the decrementing form is
        only ever attempted, never relied on, and a failed condition falls
        through to the plain delete rather than being read as "already done".
        Treating the failure as already-applied is what would leave an
        IssueBlocker outliving the Issue that named it.
        """
        if not issue_blocker.is_blocking_issue_done:
            blocker_key = self.issue_blocker_key(issue_blocker)
            applied = self.apply_idempotent_transaction([
                {"Delete": self.active_blocker_delete(blocker_key)},
                {"Update": self._build_update(
                    PK=self.issue_pk(issue_blocker.blocked_issue_space_id,
                                     issue_blocker.blocked_issue_id),
                    SK=IssueInfo.KEY_ATTRS["SK"],
                    increments={"num_active_blockers": -1},
                )},
            ], "Error sweeping issue blocker")

            if applied:
                return

        # Either the blocker was already satisfied, in which case the counter
        # was decremented when the blocking Issue went DONE, or the blocked
        # Issue is itself gone and has no counter left to hold. Both leave the
        # row to delete without a decrement.
        self.delete_row(issue_blocker)

    @retry_on_transaction_conflict()
    def handle_issue_num_active_blockers_zeroed(self, *, space_id, issue_id):
        """Handle an Issue's last active blocker having cleared.

        Triggered by an SQS event; see the IssueMixin docstring for the path.

        Conditions on the Issue still being BLOCKED with no active blockers, so
        a replay, a late event, or one overtaken by a new blocker is a no-op.
        A single-item write still conflicts with a transaction holding the
        Issue, such as add_issue_blocker's; that is retried.

        Raises:
            DDBArgsError: if space_id is invalid
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        update = self._build_update(
            PK=self.issue_pk(space_id, issue_id),
            SK=IssueInfo.KEY_ATTRS["SK"],
            expected_vals={
                "num_active_blockers": 0,
                "status": IssueStatus.BLOCKED,
            },
            **self.status_attrs(space_id=space_id, issue_id=issue_id,
                                status=IssueStatus.TODO),
        )

        try:
            self.dynamodb_client.update_item(**update)
        except ClientError as exc:
            self.raise_for_transaction_conflict(exc)

            if self.is_condition_failure(exc):
                # Missing, no longer BLOCKED, or blocked again since
                return

            self.log_client_error(exc)
            raise DDBInternalError(f"Error unblocking issue: {exc!s}") from exc

    @retry_on_transaction_conflict()
    def apply_idempotent_transaction(self, items, message):
        """Apply a transaction whose failed conditions mean "already applied".

        Retried on conflict here, at the level of the single transaction,
        rather than on the handle_* method that drives it. A sweep may apply
        hundreds of these, and restarting the whole sweep, re-querying
        included, because the 37th write met a concurrent writer would be the
        wrong unit of work.

        Contention is expected rather than exotic on this path: every blocking
        Issue reaching DONE at the same time decrements the same blocked
        Issue's counter. The SQS consumer does redeliver on an exception, but
        that costs a visibility timeout and leaves Issues BLOCKED longer than
        the conflict warranted.

        Retrying is safe because every item is conditioned: a retry
        re-evaluates against current state, and work another writer already did
        fails its condition and is treated as applied.

        Returns:
            bool: True if the transaction was applied, False if a condition
                failed. False does not always mean the work is done: a caller
                whose conditions were built from a possibly stale read must
                decide what the failure meant; see delete_blocker_for_sweep.

        Raises:
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
        """
        try:
            self.dynamodb_client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            self.raise_for_transaction_conflict(exc)

            for index in range(len(items)):
                failed, _ = self.failed_reason_item(exc, index)
                if failed:
                    return False

            self.log_client_error(exc)
            raise DDBInternalError(f"{message}: {exc!s}") from exc

        return True
