# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""A BotoCoreError from any AWS call surfaces as the matching InternalError.

A BotoCoreError, such as a connection failure, is not a ClientError, so each
call site catches it separately. Each test arranges real state, then fails only
the one client operation that call site makes, so it reaches that site and no
earlier one.
"""

import pytest
from botocore.exceptions import EndpointConnectionError

from pl8_base.errors import DDBInternalError, EventSendError, StorageInternalError
from pl8_base.types import IssueDeleted, IssueStatus
from pl8_base.util import send_event


def connection_error(*args, **kwargs):
    raise EndpointConnectionError(endpoint_url="https://aws.invalid")


class FailOn:
    """A client that raises a connection error for the named operations and
    delegates everything else to the real client."""

    def __init__(self, client, *operations):
        self._client = client
        self._operations = operations

    def __getattr__(self, name):
        if name in self._operations:
            return connection_error
        return getattr(self._client, name)


@pytest.fixture
def fail_ddb(mgr):
    def fail(*operations):
        mgr.dynamodb_client = FailOn(mgr.dynamodb_client, *operations)
    return fail


@pytest.fixture
def fail_s3(mgr):
    def fail(*operations):
        mgr.s3_client = FailOn(mgr.s3_client, *operations)
    return fail


@pytest.fixture
def space(ctv, mgr):
    return mgr.create_space(space_id=ctv.space_id, name="n", description="d",
                            creator="c")


@pytest.fixture
def make_issue(ctv, mgr, space):
    def make():
        return mgr.create_issue(space_id=ctv.space_id, title="t",
                                description="d", status=IssueStatus.TODO,
                                creator="c")
    return make


@pytest.fixture
def attachment(ctv, mgr, make_issue):
    issue = make_issue()
    attachment, _post = mgr.initiate_issue_attachment_upload(
        space_id=ctv.space_id, issue_id=issue.issue_id, name="a.txt",
        content_type="text/plain", size=3, creator="c")
    return attachment


def attachment_ids(attachment):
    return {"space_id": attachment.space_id,
            "issue_id": attachment.issue_id,
            "attachment_id": attachment.attachment_id}


class TestDynamoDB:
    def test_get_item(self, ctv, mgr, space, fail_ddb):
        fail_ddb("get_item")
        with pytest.raises(DDBInternalError, match="Error loading item"):
            mgr.get_space(space_id=ctv.space_id)

    def test_query(self, mgr, fail_ddb):
        fail_ddb("query")
        with pytest.raises(DDBInternalError, match="Error running query"):
            mgr.get_spaces()

    def test_update_item(self, ctv, mgr, space, fail_ddb):
        fail_ddb("update_item")
        with pytest.raises(DDBInternalError, match="Error updating space"):
            mgr.update_space(space_id=ctv.space_id, name="n2",
                             description="d2")

    def test_create_space(self, ctv, mgr, fail_ddb):
        fail_ddb("put_item")
        with pytest.raises(DDBInternalError, match="Error creating space"):
            mgr.create_space(space_id=ctv.space_id, name="n", description="d",
                             creator="c")

    def test_delete_space(self, ctv, mgr, space, fail_ddb):
        fail_ddb("delete_item")
        with pytest.raises(DDBInternalError, match="Error deleting space"):
            mgr.delete_space(space_id=ctv.space_id)

    def test_create_issue(self, ctv, mgr, space, fail_ddb):
        fail_ddb("transact_write_items")
        with pytest.raises(DDBInternalError, match="Error creating issue"):
            mgr.create_issue(space_id=ctv.space_id, title="t",
                             description="d", status=IssueStatus.TODO,
                             creator="c")

    def test_delete_issue(self, ctv, mgr, make_issue, fail_ddb):
        issue = make_issue()
        fail_ddb("transact_write_items")
        with pytest.raises(DDBInternalError, match="Error deleting issue"):
            mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

    def test_add_issue_blocker(self, ctv, mgr, make_issue, fail_ddb):
        blocking, blocked = make_issue(), make_issue()
        fail_ddb("transact_write_items")
        with pytest.raises(DDBInternalError,
                           match="Error adding issue blocker"):
            mgr.add_issue_blocker(
                blocking_issue_space_id=ctv.space_id,
                blocking_issue_id=blocking.issue_id,
                blocked_issue_space_id=ctv.space_id,
                blocked_issue_id=blocked.issue_id)

    def test_delete_active_issue_blocker(self, ctv, mgr, make_issue,
                                         fail_ddb):
        blocking, blocked = make_issue(), make_issue()
        fail_ddb("transact_write_items")
        with pytest.raises(DDBInternalError,
                           match="Error deleting issue blocker"):
            mgr.delete_issue_blocker(
                blocking_issue_space_id=ctv.space_id,
                blocking_issue_id=blocking.issue_id,
                blocked_issue_space_id=ctv.space_id,
                blocked_issue_id=blocked.issue_id)

    def test_delete_satisfied_issue_blocker(self, ctv, mgr, make_issue,
                                            fail_ddb):
        # With no active blocker, the first write fails its condition and
        # falls through to the second, which is the one made to fail here.
        blocking, blocked = make_issue(), make_issue()
        fail_ddb("delete_item")
        with pytest.raises(DDBInternalError,
                           match="Error deleting issue blocker"):
            mgr.delete_issue_blocker(
                blocking_issue_space_id=ctv.space_id,
                blocking_issue_id=blocking.issue_id,
                blocked_issue_space_id=ctv.space_id,
                blocked_issue_id=blocked.issue_id)

    def test_handle_issue_num_active_blockers_zeroed(self, ctv, mgr,
                                                     make_issue, fail_ddb):
        issue = make_issue()
        fail_ddb("update_item")
        with pytest.raises(DDBInternalError, match="Error unblocking issue"):
            mgr.handle_issue_num_active_blockers_zeroed(
                space_id=ctv.space_id, issue_id=issue.issue_id)

    def test_create_issue_comment(self, ctv, mgr, make_issue, fail_ddb):
        issue = make_issue()
        fail_ddb("transact_write_items")
        with pytest.raises(DDBInternalError,
                           match="Error creating issue comment"):
            mgr.create_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id, body="b",
                                     creator="c")

    def test_delete_issue_comment(self, ctv, mgr, make_issue, fail_ddb):
        issue = make_issue()
        comment = mgr.create_issue_comment(space_id=ctv.space_id,
                                           issue_id=issue.issue_id, body="b",
                                           creator="c")
        fail_ddb("transact_write_items")
        with pytest.raises(DDBInternalError,
                           match="Error deleting issue comment"):
            mgr.delete_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     comment_id=comment.comment_id)

    def test_delete_row(self, ctv, mgr, make_issue, fail_ddb):
        # handle_issue_deleted removes a comment with delete_row.
        issue = make_issue()
        mgr.create_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id, body="b",
                                 creator="c")
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)
        fail_ddb("delete_item")
        with pytest.raises(DDBInternalError,
                           match="Error deleting IssueComment"):
            mgr.handle_issue_deleted(space_id=ctv.space_id,
                                     issue_id=issue.issue_id)

    def test_apply_idempotent_transaction(self, mgr, attachment, fail_ddb):
        # Deleting a PENDING attachment goes through it.
        fail_ddb("transact_write_items")
        with pytest.raises(DDBInternalError,
                           match="Error deleting issue attachment"):
            mgr.delete_issue_attachment(**attachment_ids(attachment))

    def test_initiate_issue_attachment_upload(self, ctv, mgr, make_issue,
                                              fail_ddb):
        issue = make_issue()
        fail_ddb("transact_write_items")
        with pytest.raises(DDBInternalError,
                           match="Error initiating issue attachment upload"):
            mgr.initiate_issue_attachment_upload(
                space_id=ctv.space_id, issue_id=issue.issue_id, name="a.txt",
                content_type="text/plain", size=3, creator="c")

    def test_confirm_issue_attachment_uploaded(self, ctv, mgr, s3_client,
                                               attachment, fail_ddb):
        s3_client.put_object(Bucket=ctv.bucket_name, Key=attachment.s3_key,
                             Body=b"abc", ContentType="text/plain")
        fail_ddb("transact_write_items")
        with pytest.raises(DDBInternalError,
                           match="Error confirming issue attachment"):
            mgr.confirm_issue_attachment_uploaded(**attachment_ids(attachment))


class TestS3:
    def test_head_object(self, mgr, attachment, fail_s3):
        fail_s3("head_object")
        with pytest.raises(StorageInternalError,
                           match="Error reading attachment object"):
            mgr.confirm_issue_attachment_uploaded(**attachment_ids(attachment))

    def test_delete_object(self, mgr, attachment, fail_s3):
        fail_s3("delete_object")
        with pytest.raises(StorageInternalError,
                           match="Error deleting attachment object"):
            mgr.handle_issue_attachment_deleted(**attachment_ids(attachment))


class TestLogAwsError:
    def test_logs_a_botocore_error_without_a_response(self, mgr, caplog):
        mgr.log_aws_error(
            EndpointConnectionError(endpoint_url="https://aws.invalid"))

        assert (caplog.records[-1].getMessage()
                == "BotoCoreError (EndpointConnectionError)")


class TestSendEvent:
    def test_a_failed_call_is_an_event_send_error(self):
        event = IssueDeleted(space_id="ENG", issue_id="abc123")
        with pytest.raises(EventSendError, match="Failed to send event"):
            send_event(events_client=FailOn(None, "put_events"), event=event,
                       source="pl8", event_bus_name="bus")
