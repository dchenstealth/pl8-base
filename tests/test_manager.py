# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

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
        creator="tester",
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
    @pytest.fixture
    def exc(self):
        return ClientError(
            {"Error": {"Code": "ValidationException", "Message": "bad"}},
            "PutItem",
        )

    def test_logs_without_raising(self, mgr, exc):
        mgr.log_client_error(exc)

    def test_merges_the_response_as_a_structured_field(self, mgr, exc, caplog):
        """One logging convention across the manager: context goes in as
        keyword args, which is what the injected powertools Logger merges into
        the record and what a stdlib Logger would reject. See the README."""
        mgr.log_client_error(exc)
        record = caplog.records[-1]

        assert record.getMessage() == "ClientError (code: ValidationException)"
        assert record.response == exc.response


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


class TestBuildUpdateRemoveAttrs:
    """REMOVE, which confirm_issue_attachment_uploaded needs to drop an
    attachment's TTL attribute in the same write that marks it UPLOADED.

    A NULL would not do: DynamoDB's TTL, a sparse index and
    attribute_not_exists all read a present attribute, whatever its value."""

    def test_removes_the_attr(self, mgr, dynamodb_client, stored_info,
                              get_raw):
        dynamodb_client.update_item(**mgr._build_update(
            PK=INFO_PK, SK=INFO_SK, remove_attrs=["status_updated_at"]))

        assert "status_updated_at" not in get_raw(INFO_PK, INFO_SK)

    def test_the_attr_is_gone_not_null(self, mgr, dynamodb_client,
                                       stored_info, get_raw):
        dynamodb_client.update_item(**mgr._build_update(
            PK=INFO_PK, SK=INFO_SK, remove_attrs=["status_updated_at"]))

        row = get_raw(INFO_PK, INFO_SK)
        assert row.get("status_updated_at") is None

    def test_sets_and_removes_in_one_expression(self, mgr, dynamodb_client,
                                                stored_info, get_raw):
        # One UpdateExpression, so the set and the removal are the same atomic
        # write rather than two that could be interleaved.
        update = mgr._build_update(PK=INFO_PK, SK=INFO_SK, title="new title",
                                   remove_attrs=["status_updated_at"])

        assert update["UpdateExpression"].startswith("SET ")
        assert " REMOVE #status_updated_at" in update["UpdateExpression"]

        dynamodb_client.update_item(**update)
        row = get_raw(INFO_PK, INFO_SK)
        assert row["title"] == {"S": "new title"}
        assert "status_updated_at" not in row

    def test_still_bumps_version_and_updated_at(self, mgr, dynamodb_client,
                                                stored_info, get_raw):
        dynamodb_client.update_item(**mgr._build_update(
            PK=INFO_PK, SK=INFO_SK, remove_attrs=["status_updated_at"]))

        row = get_raw(INFO_PK, INFO_SK)
        assert row["version"] == {"N": "2"}
        assert row["updated_at"] != {"S": stored_info.updated_at}

    def test_removed_names_go_through_expression_attribute_names(self, mgr):
        # Every attr does, since "status" and "version" are reserved words and
        # the next field added would be the trap.
        update = mgr._build_update(PK=INFO_PK, SK=INFO_SK,
                                   remove_attrs=["status_updated_at"])

        assert update["ExpressionAttributeNames"]["#status_updated_at"] == \
            "status_updated_at"

    def test_no_remove_clause_without_remove_attrs(self, mgr):
        for remove_attrs in (None, [], ()):
            update = mgr._build_update(PK=INFO_PK, SK=INFO_SK, title="t",
                                       remove_attrs=remove_attrs)

            assert "REMOVE" not in update["UpdateExpression"]

    def test_removing_an_absent_attr_is_a_no_op(self, mgr, dynamodb_client,
                                                stored_info, get_raw):
        dynamodb_client.update_item(**mgr._build_update(
            PK=INFO_PK, SK=INFO_SK, remove_attrs=["never_stored"]))

        assert get_raw(INFO_PK, INFO_SK)["version"] == {"N": "2"}

    def test_removes_several_attrs(self, mgr, dynamodb_client, stored_info,
                                   get_raw):
        dynamodb_client.update_item(**mgr._build_update(
            PK=INFO_PK, SK=INFO_SK,
            remove_attrs=["status_updated_at", "num_comments"]))

        row = get_raw(INFO_PK, INFO_SK)
        assert "status_updated_at" not in row
        assert "num_comments" not in row

    def test_the_conditions_still_apply(self, mgr, dynamodb_client,
                                        stored_info, get_raw):
        with pytest.raises(ClientError):
            dynamodb_client.update_item(**mgr._build_update(
                PK=INFO_PK, SK=INFO_SK, version=99,
                remove_attrs=["status_updated_at"]))

        assert "status_updated_at" in get_raw(INFO_PK, INFO_SK)

    def test_usable_inside_a_transaction(self, mgr, dynamodb_client,
                                        stored_info, get_raw):
        dynamodb_client.transact_write_items(TransactItems=[{
            "Update": mgr._build_update(PK=INFO_PK, SK=INFO_SK,
                                        title="new title",
                                        remove_attrs=["status_updated_at"]),
        }])

        row = get_raw(INFO_PK, INFO_SK)
        assert row["title"] == {"S": "new title"}
        assert "status_updated_at" not in row
