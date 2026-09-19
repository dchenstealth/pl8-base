from botocore.exceptions import ClientError

from ..const import (
    CONDITION_FAILED_CODE,
    CONDITION_FAILED_REASON,
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
    DDBVersionConflictError,
)
from ..types import IssueBlocker, IssueInfo, IssueStatus
from ..util import (
    decode_pagination_cursor,
    encode_pagination_cursor,
    gen_issue_id,
    isotime,
    retry_on_transaction_conflict,
)


BLOCKER_SK_PREFIX = "800#BLOCKEDISSUE#"


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

    def is_condition_failure(self, exc):
        return exc.response["Error"]["Code"] == CONDITION_FAILED_CODE

    def failed_reason_item(self, exc, index):
        """The old item for one cancelled transaction entry, if it failed.

        Args:
            exc (ClientError): a TransactionCanceledException
            index (int): position of the entry in TransactItems

        Returns:
            tuple: (failed, item) where failed is whether that entry's
                condition failed and item is the parsed pre-write item, which
                is None when the row did not exist
        """
        reasons = self.cancellation_reasons(exc)
        reason = reasons[index] if index < len(reasons) else {}

        if reason.get("Code") != CONDITION_FAILED_REASON:
            return False, None

        return True, self.parse_item(reason.get("Item"))

    def issue_before_failed_write(self, exc, *, space_id, issue_id,
                                  version=None):
        """The IssueInfo as it was when a conditional write failed.

        Resolves the two failures every write shares, leaving the caller to
        classify whatever domain condition it added.

        Raises:
            DDBMissingError: if the Issue does not exist
            DDBVersionConflictError: if version is set and did not match
        """
        old = self.old_item_from_exc(exc)

        if old is None:
            raise DDBMissingError(
                f"Issue not found: {space_id}#{issue_id}") from exc

        if version is not None and old.version != version:
            raise DDBVersionConflictError(
                f"Issue {space_id}#{issue_id} changed since it was read"
            ) from exc

        return old

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
            DDBInternalError: internal database error
        """
        try:
            resp = self.dynamodb_client.update_item(**update,
                                                    ReturnValues="ALL_NEW")
        except ClientError as exc:
            if not self.is_condition_failure(exc):
                self.log_client_error(exc)
                raise DDBInternalError(
                    f"Error updating issue: {str(exc)}") from exc

            old = self.issue_before_failed_write(exc, space_id=space_id,
                                                 issue_id=issue_id,
                                                 version=version)
            if classify is not None:
                classify(old)

            self.logger.error("Unclassified condition failure updating issue",
                              space_id=space_id, issue_id=issue_id)
            raise DDBInternalError(
                f"Error updating issue: {space_id}#{issue_id}") from exc

        return self.parse_item(resp["Attributes"])

    def paginate(self, query, **kwargs):
        """Yield every item from a cursor-based query on this mixin."""
        cursor = None
        while True:
            page, cursor = query(cursor=cursor, **kwargs)
            yield from page
            if cursor is None:
                return

    def run_query(self, params, cursor=None, limit=None):
        """Run a query and return one page plus a continuation cursor.

        Returns:
            tuple: (list[BaseObject], str or None)
        """
        params = dict(params, TableName=self.table_name)
        if limit is not None:
            params["Limit"] = limit
        if cursor is not None:
            params["ExclusiveStartKey"] = decode_pagination_cursor(cursor)

        try:
            resp = self.dynamodb_client.query(**params)
        except ClientError as exc:
            self.log_client_error(exc)
            raise DDBInternalError(f"Error running query: {str(exc)}") from exc

        items = [self.parse_item(item) for item in resp.get("Items", [])]
        last_evaluated_key = resp.get("LastEvaluatedKey")
        next_cursor = (encode_pagination_cursor(last_evaluated_key)
                       if last_evaluated_key else None)

        return items, next_cursor

    # ------------------------------------------------------------------
    # Issue CRUD
    # ------------------------------------------------------------------

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

            try:
                self.dynamodb_client.put_item(
                    TableName=self.table_name,
                    Item=issue_info.serialize(ts=self.ts),
                    ConditionExpression="attribute_not_exists(#PK)",
                    ExpressionAttributeNames={"#PK": "PK"},
                )
            except ClientError as exc:
                if self.is_condition_failure(exc):
                    # The id is taken; generate another rather than overwrite
                    continue

                self.log_client_error(exc)
                raise DDBInternalError(
                    f"Error creating issue: {str(exc)}") from exc

            return issue_info

        raise DDBIdCollisionError(f"Issue ID collision after {RETRY_ISSUE_ID_COLLISIONS} tries")

    def get_issue(self, *, space_id, issue_id):
        pk = IssueInfo.KEY_ATTRS["PK"].format(space_id=space_id,
                                              issue_id=issue_id)
        sk = IssueInfo.KEY_ATTRS["SK"]
        return self.get_primary_item(PK=pk, SK=sk)

    def get_issues_by_status(self, *, space_id, status, limit=50, cursor=None):
        """One page of Issues in a status, longest-in-status first.

        Sorted by status_updated_at ascending, so the head of the first page is
        whatever has sat in this status the longest.

        Returns:
            tuple: (list[IssueInfo], str or None)
        """
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
        """
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
        """
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
            DDBMissingError: if the Issue does not exist
            DDBVersionConflictError: if version is set and did not match
            DDBInternalError: internal database error
        """
        update = self._build_update(
            PK=self.issue_pk(space_id, issue_id),
            SK=IssueInfo.KEY_ATTRS["SK"],
            version=version,
            title=title,
            description=IssueInfo.compress_value("description", description),
        )
        return self.update_issue_item(update, space_id=space_id,
                                      issue_id=issue_id, version=version)

    def transition_issue(self, *, space_id, issue_id, status):
        """Move an Issue to a new status.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            status (str): status to move to

        Returns: IssueInfo

        Raises:
            DDBMissingError: if the Issue does not exist
            DDBTerminalStatusError: if the Issue is DONE and would leave it
            DDBStillBlockedError: if the Issue still has active blockers
            DDBInternalError: internal database error
        """
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
                                      issue_id=issue_id, classify=classify)

    def delete_issue(self, *, space_id, issue_id):
        """Delete an Issue's info row.

        The IssueBlockers naming it are swept by handle_issue_deleted, which
        the stream handler drives once this write lands.

        Raises:
            DDBMissingError: if the Issue does not exist
            DDBInternalError: internal database error
        """
        try:
            self.dynamodb_client.delete_item(
                TableName=self.table_name,
                Key=self.issue_info_key(space_id, issue_id),
                ConditionExpression="attribute_exists(#PK)",
                ExpressionAttributeNames={"#PK": "PK"},
            )
        except ClientError as exc:
            if self.is_condition_failure(exc):
                raise DDBMissingError(
                    f"Issue not found: {space_id}#{issue_id}") from exc

            self.log_client_error(exc)
            raise DDBInternalError(f"Error deleting issue: {str(exc)}") from exc

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
            DDBArgsError: if the blocking and blocked issue are the same Issue
            DDBMissingError: if either Issue does not exist
            DDBBlockingIssueDoneError: if the blocking Issue is DONE
            DDBTerminalStatusError: if the blocked Issue is DONE
            DDBExistsError: if the IssueBlocker already exists
            DDBTransactionConflictError: if every attempt conflicts
        """
        if (blocking_issue_space_id == blocked_issue_space_id
                and blocking_issue_id == blocked_issue_id):
            raise DDBArgsError("Issue cannot block itself")

        # Blocking cycles between distinct Issues are permitted; the remedy is
        # delete_issue_blocker. See docs architecture/pl8/entities.md.
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
                f"Error adding issue blocker: {str(exc)}") from exc

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
            DDBMissingError: if the IssueBlocker does not exist
            DDBTransactionConflictError: if every attempt conflicts
        """
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
                    f"Error deleting issue blocker: {str(exc)}") from exc

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
            if self.is_condition_failure(exc):
                raise DDBMissingError("IssueBlocker not found") from exc

            self.log_client_error(exc)
            raise DDBInternalError(
                f"Error deleting issue blocker: {str(exc)}") from exc

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
        """
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

        No IssueBlocker may outlive either Issue it names, so both directions
        are swept. Delivery is at-least-once and unordered, so every write
        conditions on the state it expects and a duplicate event is a no-op.
        """
        # Phase 1, Issues this Issue was blocking. These rows live in this
        # Issue's own partition.
        for issue_blocker in self.paginate(self.get_issue_blocking,
                                           space_id=space_id,
                                           blocking_issue_id=issue_id):
            self.delete_blocker_for_sweep(issue_blocker)

        # Phase 2, Issues that were blocking this Issue. These rows live in the
        # blocking Issues' partitions, so they are only reachable via GSI1. No
        # counter update: the Issue holding num_active_blockers is the one
        # being deleted.
        for issue_blocker in self.paginate(self.get_issue_blockers,
                                           space_id=space_id,
                                           blocked_issue_id=issue_id):
            self.delete_blocker_row(issue_blocker)

    def delete_blocker_for_sweep(self, issue_blocker):
        """Delete one outbound IssueBlocker, decrementing if it was active."""
        blocker_key = self.issue_blocker_key(issue_blocker)

        if issue_blocker.is_blocking_issue_done:
            # Already decremented when the blocking Issue went DONE
            self.delete_blocker_row(issue_blocker)
            return

        self.apply_idempotent_transaction([
            {"Delete": self.active_blocker_delete(blocker_key)},
            {"Update": self._build_update(
                PK=self.issue_pk(issue_blocker.blocked_issue_space_id,
                                 issue_blocker.blocked_issue_id),
                SK=IssueInfo.KEY_ATTRS["SK"],
                increments={"num_active_blockers": -1},
            )},
        ], "Error sweeping issue blocker")

    def delete_blocker_row(self, issue_blocker):
        """Delete one IssueBlocker row, tolerating it already being gone."""
        blocker_key = self.issue_blocker_key(issue_blocker)

        try:
            self.dynamodb_client.delete_item(
                TableName=self.table_name,
                Key=blocker_key,
                ConditionExpression="attribute_exists(#PK)",
                ExpressionAttributeNames={"#PK": "PK"},
            )
        except ClientError as exc:
            if self.is_condition_failure(exc):
                return

            self.log_client_error(exc)
            raise DDBInternalError(
                f"Error deleting issue blocker: {str(exc)}") from exc

    def handle_issue_num_active_blockers_zeroed(self, *, space_id, issue_id):
        """Handle an Issue's last active blocker having cleared.

        Triggered by an SQS event; see the IssueMixin docstring for the path.

        Conditions on the Issue still being BLOCKED with no active blockers, so
        a replay, a late event, or one overtaken by a new blocker is a no-op.
        """
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
            if self.is_condition_failure(exc):
                # Missing, no longer BLOCKED, or blocked again since
                return

            self.log_client_error(exc)
            raise DDBInternalError(f"Error unblocking issue: {str(exc)}") from exc

    def apply_idempotent_transaction(self, items, message):
        """Apply a transaction whose failed conditions mean "already applied".

        Conflicts still propagate, since those are transient rather than a
        statement about the data.

        Raises:
            DDBTransactionConflictError: on contention
            DDBInternalError: internal database error
        """
        try:
            self.dynamodb_client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            self.raise_for_transaction_conflict(exc)

            for index in range(len(items)):
                failed, _ = self.failed_reason_item(exc, index)
                if failed:
                    return

            self.log_client_error(exc)
            raise DDBInternalError(f"{message}: {str(exc)}") from exc
