# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

from botocore.exceptions import ClientError

from ..errors import (
    DDBExistsError,
    DDBInternalError,
    DDBMissingError,
)
from ..types import IssueComment
from ..util import (
    retry_on_transaction_conflict,
    validate_creator,
    validate_space_id,
)

COMMENT_SK_PREFIX = "500#COMMENT#"


class CommentMixin:
    """IssueComment operations.

    A comment shares its Issue's partition, so a thread is one query and no GSI
    carries it. comment_id is a UUIDv7 and created_at is read back out of it,
    which is what makes sort key order creation order; see types/issue.py.

    create_issue_comment and delete_issue_comment keep the Issue's num_comments
    in step, in the same transaction as the comment write. That is what refuses
    a comment on a missing Issue, and so what keeps a comment row out of a
    partition no Issue owns. Nothing here gates deleting the Issue, unlike
    Space and issue_count: an Issue is deleted whatever its num_comments and
    IssueMixin.handle_issue_deleted sweeps the comments after it; see
    pl8-docs architecture/entities.md.

    Every public method validates its space_id before it reaches a key, the
    same as IssueMixin and SpaceMixin do.
    """

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def comment_sk(self, comment_id):
        return IssueComment.KEY_ATTRS["SK"].format(comment_id=comment_id)

    def comment_key(self, space_id, issue_id, comment_id):
        """The serialized primary key of an IssueComment row."""
        return {
            "PK": self.ts.serialize(self.issue_pk(space_id, issue_id)),
            "SK": self.ts.serialize(self.comment_sk(comment_id)),
        }

    # ------------------------------------------------------------------
    # IssueComment CRUD
    # ------------------------------------------------------------------

    @retry_on_transaction_conflict()
    def create_issue_comment(self, *, space_id, issue_id, body, creator):
        """Create an IssueComment and count it against its Issue.

        The Issue row takes a write for every comment on it, so concurrent
        comments on one Issue contend on it; a conflict is retried.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue being commented on
            body (str): comment body
            creator (str): who or what is writing the comment, recorded as
                supplied and never verified; see util.validate_creator

        Returns: IssueComment

        Raises:
            DDBArgsError: if space_id or creator is invalid, or body is not a
                string
            DDBMissingError: if the Issue does not exist
            DDBExistsError: if the generated comment id is already in use
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)
        validate_creator(creator)

        comment = IssueComment(
            space_id=space_id,
            issue_id=issue_id,
            body=body,
            creator=creator,
        )

        # Ordering is load-bearing: CancellationReasons come back positionally.
        items = [
            {"Update": self.issue_num_comments_update(space_id, issue_id, 1)},
            {"Put": {
                "TableName": self.table_name,
                "Item": comment.serialize(ts=self.ts),
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
                    f"Issue not found: {space_id}#{issue_id}") from exc

            failed, _ = self.failed_reason_item(exc, 1)
            if failed:
                # Not rerolled into a new id, unlike create_issue. A UUIDv7
                # carries 74 random bits and uuid7 counts within a millisecond
                # on top of that, so a clash is not contention to retry past
                # but a sign that ids are not being minted as assumed.
                raise DDBExistsError(
                    f"IssueComment exists: {comment.comment_id}") from exc

            self.log_client_error(exc)
            raise DDBInternalError(
                f"Error creating issue comment: {exc!s}") from exc

        return comment

    def get_issue_comment(self, *, space_id, issue_id, comment_id):
        """Load one IssueComment.

        Raises:
            DDBArgsError: if space_id is invalid
            DDBMissingError: if the IssueComment does not exist
            DDBCorruptedError: if the item cannot be parsed
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        return self.get_primary_item(PK=self.issue_pk(space_id, issue_id),
                                     SK=self.comment_sk(comment_id))

    def get_issue_comments(self, *, space_id, issue_id, limit=50, cursor=None):
        """One page of an Issue's IssueComments, oldest first.

        Sorted by sort key ascending, which is by comment_id, which is by
        creation timestamp: see types/issue.py. Nothing sorts on an updated
        timestamp, so editing a comment does not move it in the thread.

        The SK prefix is what keeps the Issue's own info and blocker rows out
        of the result. That is a key condition rather than a filter, so Limit
        counts only comment rows.

        Returns:
            tuple: (list[IssueComment], str or None)

        Raises:
            DDBArgsError: if space_id or cursor is invalid
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        return self.run_query({
            "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :sk)",
            "ExpressionAttributeNames": {"#pk": "PK", "#sk": "SK"},
            "ExpressionAttributeValues": {
                ":pk": self.ts.serialize(self.issue_pk(space_id, issue_id)),
                ":sk": self.ts.serialize(COMMENT_SK_PREFIX),
            },
            "ScanIndexForward": True,
        }, cursor=cursor, limit=limit)

    def update_issue_comment(self, *, space_id, issue_id, comment_id, body,
                             version=None):
        """Update an IssueComment's body.

        Only the body. A comment's id, creator and created_at are fixed at
        creation, and the id doubles as its place in the thread, so none of
        them is reachable from here.

        No classify hook: a comment carries no domain conditions, so a failed
        condition can only mean the row is gone or the version is stale.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            comment_id (str): id of the comment
            body (str): new comment body
            version (int or None): if set, fence the write on this version

        Returns: IssueComment

        Raises:
            DDBArgsError: if space_id is invalid, or body is not a string
            DDBMissingError: if the IssueComment does not exist
            DDBVersionConflictError: if version is set and did not match
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        update = self._build_update(
            PK=self.issue_pk(space_id, issue_id),
            SK=self.comment_sk(comment_id),
            version=version,
            body=IssueComment.compress_value("body", body),
        )
        return self.apply_update(
            update,
            entity="IssueComment",
            ref=f"{space_id}#{issue_id}#{comment_id}",
            version=version,
            log_context={"space_id": space_id, "issue_id": issue_id,
                         "comment_id": comment_id},
        )

    @retry_on_transaction_conflict()
    def delete_issue_comment(self, *, space_id, issue_id, comment_id):
        """Delete an IssueComment and uncount it from its Issue.

        Raises:
            DDBArgsError: if space_id is invalid
            DDBMissingError: if the IssueComment or its Issue does not exist
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        # Ordering is load-bearing: CancellationReasons come back positionally.
        items = [
            {"Delete": {
                "TableName": self.table_name,
                "Key": self.comment_key(space_id, issue_id, comment_id),
                "ConditionExpression": "attribute_exists(#PK)",
                "ExpressionAttributeNames": {"#PK": "PK"},
            }},
            {"Update": self.issue_num_comments_update(space_id, issue_id, -1)},
        ]

        try:
            self.dynamodb_client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            self.raise_for_transaction_conflict(exc)

            failed, _ = self.failed_reason_item(exc, 0)
            if failed:
                raise DDBMissingError(
                    "IssueComment not found: "
                    f"{space_id}#{issue_id}#{comment_id}") from exc

            # Unreachable while the invariant holds: a counted comment cannot
            # outlive the Issue that counted it.
            failed, _ = self.failed_reason_item(exc, 1)
            if failed:
                raise DDBMissingError(
                    f"Issue not found: {space_id}#{issue_id}") from exc

            self.log_client_error(exc)
            raise DDBInternalError(
                f"Error deleting issue comment: {exc!s}") from exc
