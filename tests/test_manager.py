import pytest

from botocore.exceptions import ClientError

from pl8_base.errors import (
    DDBCorruptedError,
    DDBMissingError,
)
from pl8_base.types import IssueBlocker, IssueInfo, IssueStatus


INFO_PK = "ISSUE#ENG#abc123"
INFO_SK = "100#INFO"


@pytest.fixture
def info(mgr):
    return IssueInfo(
        space_id="ENG",
        issue_id="abc123",
        title="test title",
        description="test desc",
        status=IssueStatus.TODO,
    )


@pytest.fixture
def stored_info(ctv, dynamodb_client, mgr, info):
    """An IssueInfo row written directly.

    Direct writes are used only here, where the point is to exercise the read
    and update primitives in isolation before create_issue exists. Every other
    test file arranges through the real manager methods.
    """
    dynamodb_client.put_item(TableName=ctv.table_name,
                             Item=info.serialize(ts=mgr.ts))
    return info


@pytest.fixture
def put_raw(ctv, dynamodb_client):
    """Write an arbitrary raw row, for corruption cases the API cannot produce."""
    def _put_raw(item):
        dynamodb_client.put_item(TableName=ctv.table_name, Item=item)

    return _put_raw


class TestParseItem:
    def test_none_returns_none(self, mgr):
        assert mgr.parse_item(None) is None

    def test_parses_issue_info(self, mgr, info):
        assert mgr.parse_item(info.serialize(ts=mgr.ts)) == info

    def test_parses_issue_blocker(self, mgr):
        blocker = IssueBlocker(
            blocking_issue_space_id="ENG",
            blocking_issue_id="aaa111",
            blocked_issue_space_id="OPS",
            blocked_issue_id="bbb222",
            is_blocking_issue_done=False,
        )
        assert mgr.parse_item(blocker.serialize(ts=mgr.ts)) == blocker

    def test_missing_type_is_corrupted(self, mgr, info):
        item = info.serialize(ts=mgr.ts)
        del item["type"]

        with pytest.raises(DDBCorruptedError, match="without type"):
            mgr.parse_item(item)

    def test_unknown_type_is_corrupted(self, mgr, info):
        item = info.serialize(ts=mgr.ts)
        item["type"] = {"S": "SomethingElse"}

        with pytest.raises(DDBCorruptedError, match="unknown type"):
            mgr.parse_item(item)

    def test_malformed_item_is_corrupted(self, mgr, info):
        item = info.serialize(ts=mgr.ts)
        del item["title"]

        with pytest.raises(DDBCorruptedError, match="Malformed item"):
            mgr.parse_item(item)

    def test_wrong_attribute_type_is_corrupted(self, mgr, info):
        item = info.serialize(ts=mgr.ts)
        item["num_active_blockers"] = {"S": "not a number"}

        with pytest.raises(DDBCorruptedError):
            mgr.parse_item(item)


class TestLogClientError:
    def test_logs_without_raising(self, mgr):
        exc = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "bad"}},
            "PutItem",
        )
        mgr.log_client_error(exc)


class TestGetPrimaryItem:
    def test_returns_parsed_object(self, mgr, stored_info):
        assert mgr.get_primary_item(PK=INFO_PK, SK=INFO_SK) == stored_info

    def test_missing_item_raises(self, mgr):
        with pytest.raises(DDBMissingError):
            mgr.get_primary_item(PK="ISSUE#ENG#nope00", SK=INFO_SK)

    def test_missing_sort_key_raises(self, mgr, stored_info):
        with pytest.raises(DDBMissingError):
            mgr.get_primary_item(PK=INFO_PK, SK="999#NOTHING")

    def test_untyped_row_is_corrupted(self, mgr, put_raw):
        put_raw({"PK": {"S": "ISSUE#ENG#raw001"}, "SK": {"S": INFO_SK}})

        with pytest.raises(DDBCorruptedError):
            mgr.get_primary_item(PK="ISSUE#ENG#raw001", SK=INFO_SK)

    def test_keyword_only(self, mgr, stored_info):
        with pytest.raises(TypeError):
            mgr.get_primary_item(INFO_PK, INFO_SK)


class TestBuildUpdate:
    """The shape of the dict _build_update returns.

    It has to serve two callers unchanged: update_item(**d), and
    transact_write_items(TransactItems=[{"Update": d}]). Both are exercised
    below against moto, so the dict is checked by use and not only by shape.

    _build_update only builds; the conditions it attaches fail at write time.
    Mapping a ConditionalCheckFailedException onto DDBMissingError,
    DDBVersionConflictError or a domain error belongs to the caller issuing the
    write, and is asserted through those callers in the other test modules.
    """

    def test_sets_the_given_attrs(self, mgr, dynamodb_client, stored_info, get_raw):
        update = mgr._build_update(PK=INFO_PK, SK=INFO_SK, title="new title")
        dynamodb_client.update_item(**update)

        assert get_raw(INFO_PK, INFO_SK)["title"] == {"S": "new title"}

    def test_bumps_version(self, mgr, dynamodb_client, stored_info, get_raw):
        dynamodb_client.update_item(
            **mgr._build_update(PK=INFO_PK, SK=INFO_SK, title="new title"))

        assert get_raw(INFO_PK, INFO_SK)["version"] == {"N": "2"}

    def test_sets_updated_at(self, mgr, dynamodb_client, stored_info, get_raw):
        dynamodb_client.update_item(
            **mgr._build_update(PK=INFO_PK, SK=INFO_SK, title="new title"))

        row = get_raw(INFO_PK, INFO_SK)
        assert row["updated_at"]["S"] != stored_info.updated_at
        # created_at is not a write-time field
        assert row["created_at"]["S"] == stored_info.created_at

    def test_does_not_disturb_untouched_attrs(self, mgr, dynamodb_client,
                                              stored_info, get_raw):
        dynamodb_client.update_item(
            **mgr._build_update(PK=INFO_PK, SK=INFO_SK, title="new title"))

        row = get_raw(INFO_PK, INFO_SK)
        assert row["status"] == {"S": "TODO"}
        assert row["num_active_blockers"] == {"N": "0"}

    def test_conditions_on_existence(self, mgr, dynamodb_client):
        update = mgr._build_update(PK="ISSUE#ENG#nope00", SK=INFO_SK,
                                   title="new title")

        with pytest.raises(ClientError) as excinfo:
            dynamodb_client.update_item(**update)

        assert excinfo.value.response["Error"]["Code"] == \
            "ConditionalCheckFailedException"

    def test_requests_old_values_on_condition_failure(self, mgr):
        # Missing item, stale version and an expected_vals mismatch all surface
        # as one ConditionalCheckFailedException; ALL_OLD is what lets the
        # caller tell them apart.
        update = mgr._build_update(PK=INFO_PK, SK=INFO_SK, title="new title")
        assert update["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD"

    def test_targets_the_configured_table(self, mgr, ctv):
        update = mgr._build_update(PK=INFO_PK, SK=INFO_SK, title="new title")
        assert update["TableName"] == ctv.table_name

    def test_keys_address_the_row(self, mgr):
        # DynamoDB rejects an UpdateItem that writes a key attribute, so PK and
        # SK address the row rather than being set on it.
        update = mgr._build_update(PK=INFO_PK, SK=INFO_SK, title="new title")

        assert update["Key"] == {"PK": {"S": INFO_PK}, "SK": {"S": INFO_SK}}
        assert "PK" not in update["UpdateExpression"]
        assert INFO_PK not in str(update.get("ExpressionAttributeValues", {}))

    def test_pk_is_required(self, mgr):
        with pytest.raises(TypeError):
            mgr._build_update(SK=INFO_SK, title="new title")

    def test_sk_is_required(self, mgr):
        with pytest.raises(TypeError):
            mgr._build_update(PK=INFO_PK, title="new title")

    def test_keys_are_not_optional_even_with_no_attrs(self, mgr):
        with pytest.raises(TypeError):
            mgr._build_update()

    def test_gsi_keys_are_updatable(self, mgr, dynamodb_client, stored_info,
                                    get_raw):
        # transition_issue rewrites these; they are index keys, not table keys.
        dynamodb_client.update_item(**mgr._build_update(
            PK=INFO_PK, SK=INFO_SK,
            GSI1PK="ISSUESPACESTATUS#ENG#DONE",
            GSI1SK="STATUSUPDATED#2026-01-01T00:00:09.000Z#ISSUE#abc123",
        ))

        assert get_raw(INFO_PK, INFO_SK)["GSI1PK"] == {
            "S": "ISSUESPACESTATUS#ENG#DONE",
        }

    def test_no_version_condition_unless_asked(self, mgr, dynamodb_client,
                                               stored_info, get_raw):
        # The handle_* consumers depend on this: they hold no caller's read, so
        # fencing them on version would fail replays spuriously.
        dynamodb_client.update_item(
            **mgr._build_update(PK=INFO_PK, SK=INFO_SK, title="one"))
        # Row is now at version 2, but a second unfenced write still applies.
        dynamodb_client.update_item(
            **mgr._build_update(PK=INFO_PK, SK=INFO_SK, title="two"))

        row = get_raw(INFO_PK, INFO_SK)
        assert row["title"] == {"S": "two"}
        assert row["version"] == {"N": "3"}

    def test_matching_version_applies(self, mgr, dynamodb_client, stored_info,
                                      get_raw):
        dynamodb_client.update_item(**mgr._build_update(
            PK=INFO_PK, SK=INFO_SK, version=1, title="new title"))

        assert get_raw(INFO_PK, INFO_SK)["title"] == {"S": "new title"}

    def test_stale_version_is_rejected(self, mgr, dynamodb_client, stored_info,
                                       get_raw):
        dynamodb_client.update_item(
            **mgr._build_update(PK=INFO_PK, SK=INFO_SK, title="first"))

        with pytest.raises(ClientError) as excinfo:
            dynamodb_client.update_item(**mgr._build_update(
                PK=INFO_PK, SK=INFO_SK, version=1, title="second"))

        assert excinfo.value.response["Error"]["Code"] == \
            "ConditionalCheckFailedException"
        assert get_raw(INFO_PK, INFO_SK)["title"] == {"S": "first"}

    def test_matching_expected_vals_apply(self, mgr, dynamodb_client,
                                          stored_info, get_raw):
        dynamodb_client.update_item(**mgr._build_update(
            PK=INFO_PK, SK=INFO_SK,
            expected_vals={"status": IssueStatus.TODO},
            title="new title"))

        assert get_raw(INFO_PK, INFO_SK)["title"] == {"S": "new title"}

    def test_mismatched_expected_vals_are_rejected(self, mgr, dynamodb_client,
                                                   stored_info, get_raw):
        with pytest.raises(ClientError) as excinfo:
            dynamodb_client.update_item(**mgr._build_update(
                PK=INFO_PK, SK=INFO_SK,
                expected_vals={"status": IssueStatus.DONE},
                title="new title"))

        assert excinfo.value.response["Error"]["Code"] == \
            "ConditionalCheckFailedException"
        assert get_raw(INFO_PK, INFO_SK)["title"] == {"S": "test title"}

    def test_numeric_expected_vals(self, mgr, dynamodb_client, stored_info,
                                   get_raw):
        # handle_issue_num_active_blockers_zeroed conditions on this one.
        dynamodb_client.update_item(**mgr._build_update(
            PK=INFO_PK, SK=INFO_SK,
            expected_vals={"num_active_blockers": 0},
            status=IssueStatus.TODO))

        with pytest.raises(ClientError):
            dynamodb_client.update_item(**mgr._build_update(
                PK=INFO_PK, SK=INFO_SK,
                expected_vals={"num_active_blockers": 1},
                status=IssueStatus.DONE))

    def test_boolean_expected_vals(self, mgr, dynamodb_client, ctv, get_raw):
        # delete_issue_blocker and handle_issue_done condition on this one.
        blocker = IssueBlocker(
            blocking_issue_space_id="ENG",
            blocking_issue_id="aaa111",
            blocked_issue_space_id="ENG",
            blocked_issue_id="bbb222",
            is_blocking_issue_done=False,
        )
        dynamodb_client.put_item(TableName=ctv.table_name,
                                 Item=blocker.serialize(ts=mgr.ts))

        dynamodb_client.update_item(**mgr._build_update(
            PK=blocker.PK, SK=blocker.SK,
            expected_vals={"is_blocking_issue_done": False},
            is_blocking_issue_done=True))

        assert get_raw(blocker.PK, blocker.SK)["is_blocking_issue_done"] == \
            {"BOOL": True}

    def test_usable_inside_a_transaction(self, mgr, dynamodb_client,
                                         stored_info, get_raw):
        dynamodb_client.transact_write_items(TransactItems=[{
            "Update": mgr._build_update(PK=INFO_PK, SK=INFO_SK,
                                        title="new title"),
        }])

        row = get_raw(INFO_PK, INFO_SK)
        assert row["title"] == {"S": "new title"}
        assert row["version"] == {"N": "2"}

    def test_transaction_rolls_back_on_condition_failure(self, mgr,
                                                         dynamodb_client,
                                                         stored_info, get_raw):
        with pytest.raises(ClientError):
            dynamodb_client.transact_write_items(TransactItems=[
                {"Update": mgr._build_update(PK=INFO_PK, SK=INFO_SK,
                                             title="new title")},
                {"Update": mgr._build_update(PK="ISSUE#ENG#nope00", SK=INFO_SK,
                                             title="doomed")},
            ])

        assert get_raw(INFO_PK, INFO_SK)["title"] == {"S": "test title"}

    def test_keyword_only(self, mgr):
        with pytest.raises(TypeError):
            mgr._build_update(INFO_PK, INFO_SK, title="new title")
