from boto3.dynamodb.types import TypeSerializer, TypeDeserializer

from .errors import DDBCorruptedError
from .mixins import IssueMixin
from .types import CLASS_MAP


class PL8DDB(IssueMixin):
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

    def get_primary_item(self, *, PK, SK):
        # TODO GetItem, raise DDBMissingError if missing
        pass

    def _build_update(self, *, version=None, expected_vals=None, **attrs):
        # TODO build an update dict that can be passed directly to update_item
        # or can be wrapped in {"Update": <dict>} and passed to transact_write_items
        # should support a version= condition expression for optimistic locking
        # should condition on existence for all operations
        # should support condition checking expected vals
        # Should set version += 1 and updated_at on all operations
        pass
