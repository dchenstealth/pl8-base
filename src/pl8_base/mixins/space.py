from botocore.exceptions import ClientError

from ..const import GSI1_INDEX_NAME
from ..errors import (
    DDBExistsError,
    DDBInternalError,
    DDBMissingError,
)
from ..types import SpaceInfo
from ..util import validate_space_id


class SpaceMixin:
    """Space operations.

    A Space stores Space metadata and makes every space id enumerable. It holds
    no referential integrity over Issues in either direction: create_issue does
    not check that the Space exists, and delete_space does not touch the Issues
    in that space, which is why there is no handle_space_deleted here. Callers
    that need the relationship enforced enforce it themselves.

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

    # ------------------------------------------------------------------
    # Space CRUD
    # ------------------------------------------------------------------

    def create_space(self, *, space_id, name, description):
        """Create a Space.

        No id collision retry, unlike create_issue: the id is the caller's, so a
        clash is a conflict to report rather than something to reroll past.

        Args:
            space_id (str): caller-supplied id of the space
            name (str): space display name
            description (str): space description

        Returns: SpaceInfo

        Raises:
            DDBArgsError: if space_id is invalid, or description is not a string
            DDBExistsError: if the Space already exists
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        space_info = SpaceInfo(
            space_id=space_id,
            name=name,
            description=description,
        )

        try:
            self.dynamodb_client.put_item(
                TableName=self.table_name,
                Item=space_info.serialize(ts=self.ts),
                ConditionExpression="attribute_not_exists(#PK)",
                ExpressionAttributeNames={"#PK": "PK"},
            )
        except ClientError as exc:
            if self.is_condition_failure(exc):
                raise DDBExistsError(f"Space exists: {space_id}") from exc

            self.log_client_error(exc)
            raise DDBInternalError(f"Error creating space: {str(exc)}") from exc

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

    def update_space(self, *, space_id, name, description, version=None):
        """Update a Space's name and description.

        No classify hook: a Space carries no domain conditions, so a failed
        condition can only mean the row is gone or the version is stale.

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

    def delete_space(self, *, space_id):
        """Delete a Space's info row.

        Only the Space row. The Issues in that space are left in place and
        become orphaned by design, since a Space holds no referential integrity
        over them. A caller wanting them gone deletes them itself.

        Raises:
            DDBArgsError: if space_id is invalid
            DDBMissingError: if the Space does not exist
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        try:
            self.dynamodb_client.delete_item(
                TableName=self.table_name,
                Key=self.space_info_key(space_id),
                ConditionExpression="attribute_exists(#PK)",
                ExpressionAttributeNames={"#PK": "PK"},
            )
        except ClientError as exc:
            if self.is_condition_failure(exc):
                raise DDBMissingError(f"Space not found: {space_id}") from exc

            self.log_client_error(exc)
            raise DDBInternalError(f"Error deleting space: {str(exc)}") from exc
