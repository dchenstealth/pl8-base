import msgspec

from boto3.dynamodb.types import TypeSerializer, TypeDeserializer
from botocore.exceptions import ClientError

from .const import TRANSACT_CONFLICT_REASON
from .errors import (
    DDBCorruptedError,
    DDBInternalError,
    DDBMissingError,
    DDBTransactionConflictError,
)
from .mixins import IssueMixin
from .types import CLASS_MAP
from .util import isotime


class BasePL8(IssueMixin):
    def __init__(self, *, dynamodb_client, table_name, logger):
        """Init manager.
        Args:
            dynamodb_client (boto3.dynamodb): DynamoDB client
            table_name (str): DynamoDB table name to use
            logger (aws_lambda_powertools.Logger): injected structured
                logger. Extra keyword args are merged into the emitted
                JSON log record.
        """
        self.dynamodb_client = dynamodb_client
        self.table_name = table_name
        self.logger = logger
        self.ts = TypeSerializer()
        self.td = TypeDeserializer()


    def log_client_error(self, exc):
        """Util method for structured AWS error logging

        Args:
            exc (ClientError): exception
        """
        error_code = exc.response["Error"]["Code"]
        self.logger.exception(f"ClientError (code: {error_code})",
                              extra={"Response": exc.response})

    def parse_item(self, item):
        """Util method for parsing a raw dynamodb item into a dataclass.

        Logs error details if unable to parse.

        Args:
            item (dict or None): raw dynamodb item

        Returns:
            BaseObject or None: parsed object, or None if unable to parse

        Raises:
            DDBCorruptedError: if missing or unknown type, or malformed item
        """
        if item is None:
            return
        item_type = item.get("type")
        if item_type is None:
            self.logger.error("Malformed item without type", item=item)
            raise DDBCorruptedError("Malformed item without type")

        item_type = self.td.deserialize(item_type)
        type_class = CLASS_MAP.get(item_type)

        if type_class is None:
            self.logger.error("Retrieved item with unknown type",
                              item_type=item_type, item=item)
            raise DDBCorruptedError(f"Item with unknown type: {item_type}")

        try:
            parsed = type_class.from_item(item, td=self.td)
        except Exception as exc:
            self.logger.error("Malformed item", item_type=item_type,
                              item=item, exc_info=True)
            raise DDBCorruptedError(f"Malformed item: {str(exc)}")

        return parsed

    def serialize_value(self, value):
        """Serialize one Python value to a DynamoDB AttributeValue.

        Goes through msgspec first so enums arrive as their bare values; a
        StrEnum would otherwise be serialized as itself and leak the member
        repr into anything that does not compare it as a string.

        Bytes skip that step. msgspec encodes them as a base64 *string*, which
        would quietly store a compressed attr as S instead of B and break the
        round-trip back through BaseObject.from_item.

        Args:
            value: any msgspec-encodable value

        Returns:
            dict: DynamoDB AttributeValue
        """
        if isinstance(value, (bytes, bytearray)):
            return self.ts.serialize(value)

        return self.ts.serialize(msgspec.to_builtins(value))

    def old_item_from_exc(self, exc):
        """Parse the pre-write item a failed condition returned, if any.

        Requires the write to have set ReturnValuesOnConditionCheckFailure to
        ALL_OLD. A missing item means the row did not exist, which is how a
        DDBMissingError is told apart from a condition that genuinely failed.

        Args:
            exc (ClientError): a ConditionalCheckFailedException

        Returns:
            BaseObject or None: the item as it was, or None if absent
        """
        return self.parse_item(exc.response.get("Item"))

    def cancellation_reasons(self, exc):
        """Per-item cancellation reasons from a cancelled transaction.

        Reasons come back positionally, aligned with TransactItems, so callers
        must keep their item ordering stable to read these.

        Args:
            exc (ClientError): a TransactionCanceledException

        Returns:
            list[dict]: one reason per transaction item
        """
        return exc.response.get("CancellationReasons") or []

    def raise_for_transaction_conflict(self, exc):
        """Re-raise a cancelled transaction as DDBTransactionConflictError.

        Contention is transient and the same request may be retried unchanged;
        see util.retry_on_transaction_conflict. Returns without raising if the
        cancellation was for some other reason.

        Args:
            exc (ClientError): a TransactionCanceledException

        Raises:
            DDBTransactionConflictError: if any item reports a conflict
        """
        for reason in self.cancellation_reasons(exc):
            if reason.get("Code") == TRANSACT_CONFLICT_REASON:
                raise DDBTransactionConflictError(
                    "Transaction conflict") from exc

    def get_primary_item(self, *, PK, SK):
        """Load one item by its primary key.

        Args:
            PK (str): partition key
            SK (str): sort key

        Returns:
            BaseObject: the parsed item

        Raises:
            DDBMissingError: if the item does not exist
            DDBCorruptedError: if the item cannot be parsed
            DDBInternalError: internal database error
        """
        try:
            resp = self.dynamodb_client.get_item(
                TableName=self.table_name,
                Key={
                    "PK": self.ts.serialize(PK),
                    "SK": self.ts.serialize(SK),
                },
            )
        except ClientError as exc:
            self.log_client_error(exc)
            raise DDBInternalError(f"Error loading item: {str(exc)}") from exc

        item = resp.get("Item")
        if item is None:
            raise DDBMissingError(f"Item not found: PK={PK} SK={SK}")

        return self.parse_item(item)

    def _build_update(self, *, PK, SK, version=None, expected_vals=None,
                      excluded_vals=None, increments=None, **attrs):
        """Build an update dict for update_item or transact_write_items.

        Every write bumps version and sets updated_at. The version *condition*
        is opt-in, applied only when the caller passes version=.

        Consumer-facing operations pass version= to fence a caller whose view of
        the item is stale, and get DDBVersionConflictError back if it moved.
        Nearly every background write to an Issue is semantically meaningful to a
        consumer (num_active_blockers, or the status flip that follows it), so
        these are not spurious conflicts.

        The handle_* event consumers MUST NOT pass version=. They hold no
        consumer's read, and their correctness comes from domain conditions
        instead (is_blocking_issue_done=False, status=BLOCKED,
        num_active_blockers=0), which are also what makes them idempotent under
        at-least-once, unordered delivery. Fencing them on version would make
        event replays fail spuriously.

        Conditions are only built here; nothing is raised. The conditions fail
        at write time as a single ConditionalCheckFailedException, which the
        caller issuing the write is responsible for mapping onto
        DDBMissingError, DDBVersionConflictError or a domain error.

        Args:
            PK (str): partition key of the item to update
            SK (str): sort key of the item to update
            version (int or None): if set, condition the write on this version
            expected_vals (dict or None): attr values that must match
            excluded_vals (dict or None): attr values that must NOT match.
                Needed for the DONE checks, since "is terminal" is a rule about
                what an attr must not be and expected_vals only compares equal.
            increments (dict or None): attrs to add to, rather than overwrite.
                Applied by DynamoDB, so a counter stays correct under
                concurrent writers where a read-modify-write would not.
            **attrs: attrs to set

        Returns:
            dict: update dict for update_item, or to wrap in {"Update": <dict>}
                for transact_write_items
        """
        # Every attr goes through ExpressionAttributeNames: "status" and
        # "version" are both DynamoDB reserved words, and the rest would be a
        # trap waiting for the next field added.
        expr_attr_names = {"#PK": "PK", "#version": "version"}
        expr_attr_vals = {":version_incr": self.ts.serialize(1)}
        set_clauses = ["#version = #version + :version_incr"]

        for attr, value in {"updated_at": isotime(), **attrs}.items():
            expr_attr_names[f"#{attr}"] = attr
            expr_attr_vals[f":set_{attr}"] = self.serialize_value(value)
            set_clauses.append(f"#{attr} = :set_{attr}")

        for attr, delta in (increments or {}).items():
            expr_attr_names[f"#{attr}"] = attr
            expr_attr_vals[f":incr_{attr}"] = self.serialize_value(delta)
            set_clauses.append(f"#{attr} = #{attr} + :incr_{attr}")

        condition_clauses = ["attribute_exists(#PK)"]

        if version is not None:
            expr_attr_vals[":expected_version"] = self.serialize_value(version)
            condition_clauses.append("#version = :expected_version")

        for attr, value in (expected_vals or {}).items():
            expr_attr_names[f"#{attr}"] = attr
            expr_attr_vals[f":expected_{attr}"] = self.serialize_value(value)
            condition_clauses.append(f"#{attr} = :expected_{attr}")

        for attr, value in (excluded_vals or {}).items():
            expr_attr_names[f"#{attr}"] = attr
            expr_attr_vals[f":excluded_{attr}"] = self.serialize_value(value)
            condition_clauses.append(f"#{attr} <> :excluded_{attr}")

        return {
            "TableName": self.table_name,
            "Key": {
                "PK": self.ts.serialize(PK),
                "SK": self.ts.serialize(SK),
            },
            "UpdateExpression": "SET " + ", ".join(set_clauses),
            "ConditionExpression": " AND ".join(condition_clauses),
            "ExpressionAttributeNames": expr_attr_names,
            "ExpressionAttributeValues": expr_attr_vals,
            # Missing, stale version and an expected_vals mismatch all surface
            # as one ConditionalCheckFailedException; the old item is the only
            # way to tell them apart. In a transaction the per-item reasons come
            # back positionally in CancellationReasons, so item ordering must
            # stay stable.
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }
