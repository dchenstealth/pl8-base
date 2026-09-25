# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

from botocore.exceptions import ClientError

from ..const import GSI1_INDEX_NAME
from ..errors import (
    DDBExistsError,
    DDBInternalError,
    DDBMissingError,
    DDBSpaceNotEmptyError,
)
from ..types import SpaceInfo
from ..util import (
    retry_on_transaction_conflict,
    validate_creator,
    validate_space_id,
)


class SpaceMixin:
    """Space operations.

    A Space stores Space metadata and makes every space id enumerable. It also
    holds referential integrity over its Issues through issue_count:
    create_issue and delete_issue adjust it in the same transaction as the
    Issue write, so an Issue cannot be created in a missing Space, and
    delete_space conditions on it being 0, so a Space cannot be deleted out
    from under its Issues. delete_space never touches the Issues themselves,
    which is why there is no handle_space_deleted here.

    space_id is caller-supplied rather than generated, so unlike an issue_id
    nothing has already constrained it. Every method here validates it before it
    reaches a key; see util.validate_space_id.
    """

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def space_pk(self, space_id):
        return SpaceInfo.KEY_ATTRS["PK"].format(space_id=space_id)

    def space_info_key(self, space_id):
        return {
            "PK": self.ts.serialize(self.space_pk(space_id)),
            "SK": self.ts.serialize(SpaceInfo.KEY_ATTRS["SK"]),
        }

    def space_issue_count_update(self, space_id, delta):
        """Update dict adding delta to a Space's issue_count.

        For a transaction alongside the Issue write it counts. The condition on
        the Space existing is also what refuses an Issue whose Space is
        missing.

        Built by hand rather than with _build_update, which bumps version and
        updated_at. A Space's version fences update_space's name and
        description edits, and the count is bookkeeping rather than a consumer
        edit, so moving it must not fail a concurrent update_space(version=...)
        with a spurious DDBVersionConflictError.
        """
        return {
            "TableName": self.table_name,
            "Key": self.space_info_key(space_id),
            "UpdateExpression": "ADD #issue_count :delta",
            "ConditionExpression": "attribute_exists(#PK)",
            "ExpressionAttributeNames": {
                "#PK": "PK",
                "#issue_count": "issue_count",
            },
            "ExpressionAttributeValues": {
                ":delta": self.serialize_value(delta),
            },
        }

    # ------------------------------------------------------------------
    # Space CRUD
    # ------------------------------------------------------------------

    @retry_on_transaction_conflict()
    def create_space(self, *, space_id, name, description, creator):
        """Create a Space.

        No id collision retry, unlike create_issue: the id is the caller's, so a
        clash is a conflict to report rather than something to reroll past. A
        transaction conflict is retried: create_issue locks the Space key even
        while no Space exists there.

        Args:
            space_id (str): caller-supplied id of the space
            name (str): space display name
            description (str): space description
            creator (str): who or what is creating the Space, recorded as
                supplied and never verified; see util.validate_creator

        Returns: SpaceInfo

        Raises:
            DDBArgsError: if space_id or creator is invalid, or description is
                not a string
            DDBExistsError: if the Space already exists
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)
        validate_creator(creator)

        space_info = SpaceInfo(
            space_id=space_id,
            name=name,
            description=description,
            creator=creator,
        )

        try:
            self.dynamodb_client.put_item(
                TableName=self.table_name,
                Item=space_info.serialize(ts=self.ts),
                ConditionExpression="attribute_not_exists(#PK)",
                ExpressionAttributeNames={"#PK": "PK"},
            )
        except ClientError as exc:
            self.raise_for_transaction_conflict(exc)

            if self.is_condition_failure(exc):
                raise DDBExistsError(f"Space exists: {space_id}") from exc

            self.log_client_error(exc)
            raise DDBInternalError(f"Error creating space: {exc!s}") from exc

        return space_info

    def get_space(self, *, space_id):
        """Load one Space.

        Raises:
            DDBArgsError: if space_id is invalid
            DDBMissingError: if the Space does not exist
            DDBCorruptedError: if the item cannot be parsed
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        return self.get_primary_item(PK=self.space_pk(space_id),
                                     SK=SpaceInfo.KEY_ATTRS["SK"])

    def get_spaces(self, *, limit=50, cursor=None):
        """One page of every Space, sorted by space_id.

        Every Space shares one GSI1 partition, so enumeration is a single query.
        GSI1PK is a placeholder-free template and is used as-is.

        Returns:
            tuple: (list[SpaceInfo], str or None)

        Raises:
            DDBArgsError: if cursor is invalid
            DDBInternalError: internal database error
        """
        return self.run_query({
            "IndexName": GSI1_INDEX_NAME,
            "KeyConditionExpression": "#gsi1pk = :gsi1pk",
            "ExpressionAttributeNames": {"#gsi1pk": "GSI1PK"},
            "ExpressionAttributeValues": {
                ":gsi1pk": self.ts.serialize(SpaceInfo.KEY_ATTRS["GSI1PK"]),
            },
            "ScanIndexForward": True,
        }, cursor=cursor, limit=limit)

    @retry_on_transaction_conflict()
    def update_space(self, *, space_id, name, description, version=None):
        """Update a Space's name and description.

        No classify hook: a Space carries no domain conditions, so a failed
        condition can only mean the row is gone or the version is stale.

        Issue creates and deletes hold the Space row in a transaction, and a
        write landing meanwhile is rejected; that conflict is retried.

        Args:
            space_id (str): id of the space
            name (str): new space display name
            description (str): new space description
            version (int or None): if set, fence the write on this version

        Returns: SpaceInfo

        Raises:
            DDBArgsError: if space_id is invalid, or description is not a string
            DDBMissingError: if the Space does not exist
            DDBVersionConflictError: if version is set and did not match
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        update = self._build_update(
            PK=self.space_pk(space_id),
            SK=SpaceInfo.KEY_ATTRS["SK"],
            version=version,
            name=name,
            description=SpaceInfo.compress_value("description", description),
        )
        return self.apply_update(update, entity="Space", ref=space_id,
                                 version=version,
                                 log_context={"space_id": space_id})

    @retry_on_transaction_conflict()
    def delete_space(self, *, space_id):
        """Delete a Space's info row.

        Refused while the Space has any Issues, whatever their status. The
        check is issue_count rather than a query, so it is atomic with the
        delete: an Issue created concurrently either lands first and fails the
        condition, or finds the Space gone and fails its own. If the create's
        transaction is still in flight the delete is rejected as a conflict
        and retried.

        Raises:
            DDBArgsError: if space_id is invalid
            DDBMissingError: if the Space does not exist
            DDBSpaceNotEmptyError: if the Space still has Issues
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        try:
            self.dynamodb_client.delete_item(
                TableName=self.table_name,
                Key=self.space_info_key(space_id),
                ConditionExpression="attribute_exists(#PK) AND #issue_count = :zero",
                ExpressionAttributeNames={
                    "#PK": "PK",
                    "#issue_count": "issue_count",
                },
                ExpressionAttributeValues={":zero": self.serialize_value(0)},
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
        except ClientError as exc:
            self.raise_for_transaction_conflict(exc)

            if self.is_condition_failure(exc):
                old = self.old_item_from_exc(exc)
                if old is None:
                    raise DDBMissingError(
                        f"Space not found: {space_id}") from exc
                raise DDBSpaceNotEmptyError(
                    f"Space {space_id} has {old.issue_count} Issues") from exc

            self.log_client_error(exc)
            raise DDBInternalError(f"Error deleting space: {exc!s}") from exc
