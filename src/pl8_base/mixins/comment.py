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
    validate_comment_id,
    validate_creator,
    validate_space_id,
)

COMMENT_SK_PREFIX = "500#COMMENT#"

# Upper bound of the comment group in an Issue's partition, for the sort key
# range get_issue_comments_after builds. "$" is the character after "#", so
# incrementing the prefix's own final character gives a key that sorts above
# every 500#COMMENT#<id> and below anything numbered higher. Derived from the
# prefix rather than spelled out, so the bound cannot drift away from the keys
# it is bounding.
COMMENT_SK_GROUP_END = COMMENT_SK_PREFIX[:-1] + "$"

# Lowest code point there is, appended to a sort key to make an inclusive bound
# exclusive: nothing sorts between a key and that key plus a NUL.
SK_EXCLUSIVE_SUFFIX = "\u0000"


class CommentMixin:
    """IssueComment operations.

    A comment shares its Issue's partition, so a thread is one query and no GSI
    carries it. comment_id is a UUIDv7 and created_at is read back out of it,
    which is what makes sort key order creation order; see types/issue.py.

    A comment MAY also have IssueAttachments linked to it, which it counts in
    num_attachments; those rows and that counter are AttachmentMixin's, and
    deleting a comment sweeps them through handle_issue_comment_deleted.

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

    def get_issue_comments(self, *, space_id, issue_id, limit=50, cursor=None,
                           ascending=True):
        """One page of an Issue's IssueComments, oldest first by default.

        Sorted by sort key, which is by comment_id, which is by creation
        timestamp: see types/issue.py. Nothing sorts on an updated timestamp, so
        editing a comment does not move it in the thread.

        Oldest first is the default rather than the invariant it once was:
        ascending=False reads the thread newest first, which is what a caller
        showing the latest activity on a long thread wants, and what lets it
        page from the end without walking the whole thread. Only the direction
        changes; the ordering is still by comment_id either way.

        The SK prefix is what keeps the Issue's own info, attachment and blocker
        rows out of the result. That is a key condition rather than a filter, so
        Limit counts only comment rows.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            limit (int): maximum rows per page
            cursor (str or None): pagination cursor from a previous page
            ascending (bool): oldest first when True, newest first when False

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
            "ScanIndexForward": ascending,
        }, cursor=cursor, limit=limit)

    def get_issue_comments_after(self, *, space_id, issue_id,
                                 last_comment_id=None, limit=50, cursor=None):
        """One page of an Issue's IssueComments newer than a given comment.

        For a caller syncing a thread it has already partly read: it holds the
        id of the last comment it saw and wants what has been written since.

        Always ascending, with no direction to choose, unlike
        get_issue_comments above: "after" has only one sensible order, since the
        caller is extending a thread it already holds from the point it
        stopped. Reading that range backwards would hand it the newest comment
        first and leave it to reverse the page itself.

        The range is a BETWEEN on the sort key:

            :start  500#COMMENT#<last_comment_id>\u0000
            :end    500#COMMENT$

        BETWEEN is inclusive at both ends, so the start bound carries a NUL to
        push it just past the named comment's own key and exclude it; nothing
        sorts between a key and that key plus a NUL. With no last_comment_id
        the start is the bare prefix, which is below every comment key, so the
        whole thread comes back.

        The upper bound is the subtle half, and it is needed at all because
        DynamoDB permits exactly one sort key range condition: `>` cannot be
        combined with a begins_with to keep the range inside the comment group,
        so the range has to bound itself. An unbounded `>` would run straight
        past the comments into the other row types sharing the partition, and a
        "new comments" call would hand back parsed IssueAttachments and
        IssueBlockers.

        COMMENT_SK_GROUP_END is that bound, and it is derived from the comment
        prefix alone: "$" is the character after "#", so "500#COMMENT$" sorts
        above every 500#COMMENT#<id> key and below any key with a higher group
        number. It is the end of the comment group, not the start of whatever
        happens to sit above it, so nothing here has to be revisited when a row
        type is added to or removed from the partition; the numeric group
        prefixes are what make that true. See dchenstealth/docs
        guidelines/dynamodb_keys.md.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            last_comment_id (str or None): id of the newest comment the caller
                already has, excluded from the result; None for the whole
                thread
            limit (int): maximum rows per page
            cursor (str or None): pagination cursor from a previous page

        Returns:
            tuple: (list[IssueComment], str or None)

        Raises:
            DDBArgsError: if space_id or cursor is invalid, or last_comment_id
                is given and is not a UUIDv7
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        if last_comment_id is None:
            start = COMMENT_SK_PREFIX
        else:
            # It composes a sort key bound, so an id carrying a "#" or a
            # newline would move the range rather than fail to match in it.
            validate_comment_id(last_comment_id)
            start = (f"{COMMENT_SK_PREFIX}{last_comment_id}"
                     f"{SK_EXCLUSIVE_SUFFIX}")

        return self.run_query({
            "KeyConditionExpression":
                "#pk = :pk AND #sk BETWEEN :start AND :end",
            "ExpressionAttributeNames": {"#pk": "PK", "#sk": "SK"},
            "ExpressionAttributeValues": {
                ":pk": self.ts.serialize(self.issue_pk(space_id, issue_id)),
                ":start": self.ts.serialize(start),
                ":end": self.ts.serialize(COMMENT_SK_GROUP_END),
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

            # Reachable in the window between an Issue being deleted and
            # handle_issue_deleted sweeping its comments: the comment row is
            # still there, but the Issue holding num_comments is not. The
            # transaction rolls back, so the row stays for the sweep to remove,
            # and the caller is told the Issue is gone rather than the comment.
            failed, _ = self.failed_reason_item(exc, 1)
            if failed:
                raise DDBMissingError(
                    f"Issue not found: {space_id}#{issue_id}") from exc

            self.log_client_error(exc)
            raise DDBInternalError(
                f"Error deleting issue comment: {exc!s}") from exc
