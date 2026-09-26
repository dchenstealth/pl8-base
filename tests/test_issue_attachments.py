# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""IssueAttachment tests.

State is arranged through the real manager methods, as everywhere else in this
suite, which for an attachment means the full three steps a caller takes:
initiate, upload the bytes to the moto-backed bucket, confirm. Skipping to a
hand-written UPLOADED row would test a state the implementation cannot reach.

The manager and the bucket come from conftest, which configures storage on the
shared manager so any module can arrange an attachment. That the storage
arguments really are optional, which is what lets a consumer who never touches
attachments skip configuring S3, is covered by TestStorageIsOptional below,
which builds a manager without them.

Presigning needs no moto: it is a local computation over the credentials
conftest's aws_environment fixture sets, and makes no call to S3. moto is only
needed for the head_object confirm reads and the delete_object the event
handler makes.
"""

import base64
import json
import uuid

import pytest

from pl8_base.const import (
    ATTACHMENT_PENDING_TTL_SECONDS,
    PRESIGN_EXPIRY_SECONDS,
)
from pl8_base.errors import (
    DDBArgsError,
    DDBAttachmentStatusError,
    DDBExistsError,
    DDBMissingError,
    DDBVersionConflictError,
    StorageInternalError,
    StorageObjectMissingError,
)
from pl8_base.manager import BasePL8
from pl8_base.types import (
    AttachmentStatus,
    IssueAttachment,
    IssueBlocker,
    IssueComment,
    IssueStatus,
)
from pl8_base.util import isotime_from_uuid7

pytestmark = pytest.mark.usefixtures("spaces")

DECLARED_SIZE = 11
DECLARED_TYPE = "application/pdf"


@pytest.fixture
def issue(ctv, mgr):
    """A freshly created TODO Issue to hang attachments off."""
    return mgr.create_issue(space_id=ctv.space_id,
                            title="test title",
                            description="test desc",
                            status=IssueStatus.TODO,
                            creator="tester")


@pytest.fixture
def comment(ctv, mgr, issue):
    return mgr.create_issue_comment(space_id=ctv.space_id,
                                    issue_id=issue.issue_id,
                                    body="first note",
                                    creator="alice")


@pytest.fixture
def initiate(ctv, mgr, issue):
    """Start an upload, returning (attachment, presigned post)."""
    def _initiate(*, name="report.pdf", content_type=DECLARED_TYPE,
                  size=DECLARED_SIZE, creator="alice", comment_id=None,
                  issue_id=None):
        return mgr.initiate_issue_attachment_upload(
            space_id=ctv.space_id,
            issue_id=issue_id or issue.issue_id,
            name=name, content_type=content_type, size=size, creator=creator,
            comment_id=comment_id)

    return _initiate


@pytest.fixture
def upload(ctv, s3_client):
    """Put the bytes a caller would have POSTed with the presigned form."""
    def _upload(attachment, *, body=b"hello world", content_type=None):
        s3_client.put_object(Bucket=ctv.bucket_name,
                             Key=attachment.s3_key,
                             Body=body,
                             ContentType=content_type
                             or attachment.content_type)

    return _upload


@pytest.fixture
def attach(ctv, mgr, initiate, upload):
    """A fully attached file: initiated, uploaded and confirmed."""
    def _attach(*, body=b"hello world", content_type=None, **kwargs):
        attachment, _ = initiate(**kwargs)
        upload(attachment, body=body, content_type=content_type)
        return mgr.confirm_issue_attachment_uploaded(
            space_id=ctv.space_id,
            issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)

    return _attach


@pytest.fixture
def object_keys(ctv, s3_client):
    """Every key in the attachments bucket."""
    def _object_keys():
        resp = s3_client.list_objects_v2(Bucket=ctv.bucket_name)
        return [obj["Key"] for obj in resp.get("Contents", [])]

    return _object_keys


@pytest.fixture
def presign_calls(mgr, monkeypatch):
    """Record what the manager asks to have signed, then sign it for real.

    Signing happens locally against the credentials, so the recorded kwargs are
    the whole contract: what goes into the policy is exactly what S3 will
    enforce on the upload.
    """
    calls = []
    real = mgr.s3_client.generate_presigned_post

    def _spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(mgr.s3_client, "generate_presigned_post", _spy)
    return calls


def policy_of(post):
    """The signed policy document out of a presigned POST's fields."""
    return json.loads(base64.b64decode(post["fields"]["policy"]))


def reload_issue(mgr, ctv, issue):
    return mgr.get_issue(space_id=ctv.space_id, issue_id=issue.issue_id)


def reload_comment(mgr, ctv, issue, comment):
    return mgr.get_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)


def attachment_row(get_raw, ctv, attachment):
    return get_raw(f"ISSUE#{ctv.space_id}#{attachment.issue_id}",
                   f"600#ATTACHMENT#{attachment.attachment_id}")


class TestInitiateIssueAttachmentUpload:
    def test_returns_a_pending_attachment(self, ctv, issue, initiate):
        attachment, _ = initiate()

        assert attachment.space_id == ctv.space_id
        assert attachment.issue_id == issue.issue_id
        assert attachment.name == "report.pdf"
        assert attachment.creator == "alice"
        assert attachment.content_type == DECLARED_TYPE
        assert attachment.size == DECLARED_SIZE
        assert attachment.status is AttachmentStatus.PENDING
        assert attachment.comment_id is None
        assert attachment.version == 1

    def test_returned_object_matches_what_was_stored(self, ctv, mgr, initiate):
        attachment, _ = initiate()

        loaded, _ = mgr.get_issue_attachment(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)
        assert loaded == attachment

    def test_generates_a_uuidv7_id(self, initiate):
        attachment, _ = initiate()

        assert uuid.UUID(attachment.attachment_id).version == 7

    def test_created_at_comes_from_the_id(self, initiate):
        # The id is what orders the list, so created_at is read back out of it
        # rather than taken from a second, independent clock reading.
        attachment, _ = initiate()

        assert isotime_from_uuid7(attachment.attachment_id) == \
            attachment.created_at

    def test_writes_exactly_one_row(self, initiate, scan_issue_rows):
        initiate()

        # The Issue's own info row, plus the attachment
        assert len(scan_issue_rows()) == 2

    def test_row_keys_are_exact(self, ctv, issue, initiate, get_raw):
        attachment, _ = initiate()

        row = attachment_row(get_raw, ctv, attachment)
        assert row["PK"] == {"S": f"ISSUE#{ctv.space_id}#{issue.issue_id}"}
        assert row["SK"] == {
            "S": f"600#ATTACHMENT#{attachment.attachment_id}"}

    def test_sorts_between_the_comments_and_the_blockers(self, initiate):
        attachment, _ = initiate()

        assert IssueComment.KEY_ATTRS["SK"] < attachment.SK
        assert attachment.SK < IssueBlocker.KEY_ATTRS["SK"]

    def test_sets_an_expiry(self, initiate, get_raw, ctv):
        # A caller that never uploads or never confirms is the only party that
        # knew about this row, so DynamoDB's TTL is what reclaims it.
        attachment, _ = initiate()

        row = attachment_row(get_raw, ctv, attachment)
        assert attachment.expires_at is not None
        assert row["expires_at"] == {"N": str(attachment.expires_at)}

    def test_the_expiry_is_a_day_out(self, initiate):
        import time

        attachment, _ = initiate()

        expected = int(time.time()) + ATTACHMENT_PENDING_TTL_SECONDS
        # Generous window: the only claim is that the TTL is the configured
        # offset from now rather than an unrelated timestamp.
        assert abs(attachment.expires_at - expected) < 60

    def test_moves_no_counter(self, ctv, mgr, issue, comment, initiate):
        # The departure from the comment and Space idiom: an authorized upload
        # is not an attachment yet, so nothing is counted until confirm.
        initiate(comment_id=comment.comment_id)

        assert reload_issue(mgr, ctv, issue).num_attachments == 0
        assert reload_comment(mgr, ctv, issue, comment).num_attachments == 0

    def test_uploads_nothing(self, initiate, object_keys):
        # PL8 never handles the bytes; the caller POSTs them itself.
        initiate()

        assert object_keys() == []

    def test_a_missing_issue_is_rejected(self, ctv, mgr):
        with pytest.raises(DDBMissingError, match="Issue not found"):
            mgr.initiate_issue_attachment_upload(
                space_id=ctv.space_id, issue_id="nosuch", name="x.pdf",
                content_type=DECLARED_TYPE, size=DECLARED_SIZE,
                creator="alice")

    def test_a_missing_comment_is_rejected(self, ctv, mgr, issue):
        # Distinguished from the missing Issue above by position in
        # CancellationReasons, not by a second read.
        with pytest.raises(DDBMissingError, match="IssueComment not found"):
            mgr.initiate_issue_attachment_upload(
                space_id=ctv.space_id, issue_id=issue.issue_id, name="x.pdf",
                content_type=DECLARED_TYPE, size=DECLARED_SIZE,
                creator="alice",
                comment_id="0199f3a1-0000-7000-8000-000000000001")

    def test_a_missing_issue_and_a_missing_comment_are_told_apart(self, ctv,
                                                                 mgr):
        # Both ConditionChecks fail at once here, and the Issue is the one
        # reported: the reasons are read in item order, so the outermost
        # missing parent wins.
        with pytest.raises(DDBMissingError, match="Issue not found"):
            mgr.initiate_issue_attachment_upload(
                space_id=ctv.space_id, issue_id="nosuch", name="x.pdf",
                content_type=DECLARED_TYPE, size=DECLARED_SIZE,
                creator="alice",
                comment_id="0199f3a1-0000-7000-8000-000000000001")

    def test_a_rejected_initiate_writes_nothing(self, ctv, mgr,
                                               scan_issue_rows):
        with pytest.raises(DDBMissingError):
            mgr.initiate_issue_attachment_upload(
                space_id=ctv.space_id, issue_id="nosuch", name="x.pdf",
                content_type=DECLARED_TYPE, size=DECLARED_SIZE,
                creator="alice")

        assert scan_issue_rows() == []

    def test_a_deleted_issue_takes_no_new_attachments(self, ctv, mgr, issue,
                                                     initiate):
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

        with pytest.raises(DDBMissingError, match="Issue not found"):
            initiate()

    def test_an_id_clash_is_reported_not_rerolled(self, ctv, mgr, initiate,
                                                  monkeypatch):
        attachment, _ = initiate()
        monkeypatch.setattr("pl8_base.types.issue.uuid7",
                            lambda: uuid.UUID(attachment.attachment_id))

        with pytest.raises(DDBExistsError, match="IssueAttachment exists"):
            initiate()

    @pytest.mark.parametrize("kwargs, match", [
        ({"name": ""}, "Attachment name is empty"),
        ({"name": 'in"jected.pdf'}, "invalid characters"),
        ({"name": "line\nbreak.pdf"}, "invalid characters"),
        ({"name": "x" * 200}, "too long"),
        ({"content_type": "notamediatype"}, "Invalid content type"),
        ({"content_type": ""}, "Content type is empty"),
        ({"size": 0}, "at least 1 byte"),
        ({"size": 1024 ** 4}, "too large"),
        ({"creator": ""}, "Creator is empty"),
        ({"comment_id": "not-a-uuid"}, "Invalid UUID"),
    ])
    def test_rejects_invalid_arguments(self, initiate, kwargs, match):
        with pytest.raises(DDBArgsError, match=match):
            initiate(**kwargs)

    def test_a_rejected_argument_writes_nothing(self, initiate,
                                                scan_issue_rows):
        with pytest.raises(DDBArgsError):
            initiate(name='bad"name')

        # Only the Issue's own info row
        assert len(scan_issue_rows()) == 1

    def test_rejects_an_invalid_space_id(self, mgr, issue):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.initiate_issue_attachment_upload(
                space_id="ENG#OPS", issue_id=issue.issue_id, name="x.pdf",
                content_type=DECLARED_TYPE, size=DECLARED_SIZE,
                creator="alice")

    def test_keyword_only(self, ctv, mgr, issue):
        with pytest.raises(TypeError):
            mgr.initiate_issue_attachment_upload(
                ctv.space_id, issue.issue_id, "x.pdf", DECLARED_TYPE,
                DECLARED_SIZE, "alice")


class TestUnlinkedAttachmentIsNotIndexed:
    """The sparse GSI1, which is the failure a linked-only test would miss.

    An unlinked attachment has no comment to be indexed under, and DynamoDB
    rejects a PutItem whose index key attribute is present with type NULL, so a
    row that serialized comment_id's None into GSI1PK could not be written at
    all. The whole path has to be exercised against the real table.
    """

    def test_the_row_is_written(self, initiate, get_raw, ctv):
        attachment, _ = initiate()

        assert attachment_row(get_raw, ctv, attachment) is not None

    def test_the_gsi_keys_are_absent_not_null(self, initiate, get_raw, ctv):
        attachment, _ = initiate()

        row = attachment_row(get_raw, ctv, attachment)
        assert "GSI1PK" not in row
        assert "GSI1SK" not in row

    def test_the_object_carries_no_gsi_keys_either(self, initiate):
        attachment, _ = initiate()

        assert attachment.GSI1PK is None
        assert attachment.GSI1SK is None

    def test_serialize_omits_them(self, mgr, initiate):
        attachment, _ = initiate()

        item = attachment.serialize(ts=mgr.ts)
        assert "GSI1PK" not in item
        assert "GSI1SK" not in item

    def test_no_key_carries_the_string_none(self, initiate, get_raw, ctv):
        # The failure the type is guarding against: a rendered
        # "COMMENTATTACHMENT#ENG#abc123#None" would index every unlinked
        # attachment in the table under one garbage partition.
        attachment, _ = initiate()

        row = attachment_row(get_raw, ctv, attachment)
        assert all("None" not in value.get("S", "")
                   for value in row.values())

    def test_comment_id_still_round_trips_as_null(self, ctv, mgr, initiate,
                                                  get_raw):
        # Only key attrs are dropped when None. comment_id is an ordinary
        # field, so it must survive as a NULL and come back as None.
        attachment, _ = initiate()

        row = attachment_row(get_raw, ctv, attachment)
        assert row["comment_id"] == {"NULL": True}

        loaded, _ = mgr.get_issue_attachment(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)
        assert loaded.comment_id is None

    def test_it_is_not_in_the_index(self, ctv, mgr, issue, comment, initiate):
        initiate()

        found, _ = mgr.get_issue_comment_attachments(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            comment_id=comment.comment_id)
        assert found == []

    def test_it_is_still_the_issues_attachment(self, ctv, mgr, issue,
                                               initiate):
        attachment, _ = initiate()

        page, _ = mgr.get_issue_attachments(space_id=ctv.space_id,
                                            issue_id=issue.issue_id)
        assert [a.attachment_id for a in page] == [attachment.attachment_id]


class TestLinkedAttachmentIsIndexed:
    def test_renders_the_gsi_keys(self, ctv, issue, comment, initiate):
        attachment, _ = initiate(comment_id=comment.comment_id)

        assert attachment.GSI1PK == (
            f"COMMENTATTACHMENT#{ctv.space_id}#{issue.issue_id}"
            f"#{comment.comment_id}")
        assert attachment.GSI1SK == \
            f"600#ATTACHMENT#{attachment.attachment_id}"

    def test_the_row_carries_them(self, ctv, comment, initiate, get_raw):
        attachment, _ = initiate(comment_id=comment.comment_id)

        row = attachment_row(get_raw, ctv, attachment)
        assert row["GSI1PK"] == {"S": attachment.GSI1PK}
        assert row["GSI1SK"] == {"S": attachment.GSI1SK}

    def test_reachable_through_the_index(self, ctv, mgr, issue, comment,
                                        initiate):
        attachment, _ = initiate(comment_id=comment.comment_id)

        found, cursor = mgr.get_issue_comment_attachments(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            comment_id=comment.comment_id)

        assert [a.attachment_id for a in found] == [attachment.attachment_id]
        assert cursor is None

    def test_another_comments_attachments_are_separate(self, ctv, mgr, issue,
                                                       comment, initiate):
        other = mgr.create_issue_comment(space_id=ctv.space_id,
                                         issue_id=issue.issue_id,
                                         body="other", creator="alice")
        initiate(comment_id=comment.comment_id)

        found, _ = mgr.get_issue_comment_attachments(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            comment_id=other.comment_id)
        assert found == []

    def test_returns_them_oldest_first(self, ctv, mgr, issue, comment,
                                       initiate):
        first, _ = initiate(name="a.pdf", comment_id=comment.comment_id)
        second, _ = initiate(name="b.pdf", comment_id=comment.comment_id)

        found, _ = mgr.get_issue_comment_attachments(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            comment_id=comment.comment_id)

        assert [a.attachment_id for a in found] == [first.attachment_id,
                                                    second.attachment_id]

    def test_can_be_read_newest_first(self, ctv, mgr, issue, comment,
                                      initiate):
        first, _ = initiate(name="a.pdf", comment_id=comment.comment_id)
        second, _ = initiate(name="b.pdf", comment_id=comment.comment_id)

        found, _ = mgr.get_issue_comment_attachments(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            comment_id=comment.comment_id, ascending=False)

        assert [a.attachment_id for a in found] == [second.attachment_id,
                                                    first.attachment_id]

    def test_rejects_an_invalid_space_id(self, mgr, issue, comment):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.get_issue_comment_attachments(space_id="ENG#OPS",
                                              issue_id=issue.issue_id,
                                              comment_id=comment.comment_id)


class TestPresignedUpload:
    def test_signs_an_exact_size_range(self, initiate):
        # The caller declared the size, so S3 is told to accept that length and
        # no other.
        _, post = initiate(size=DECLARED_SIZE)

        assert ["content-length-range", DECLARED_SIZE, DECLARED_SIZE] in \
            policy_of(post)["conditions"]

    def test_signs_the_content_type_as_a_condition_and_a_field(self, initiate):
        _, post = initiate()

        assert {"Content-Type": DECLARED_TYPE} in policy_of(post)["conditions"]
        assert post["fields"]["Content-Type"] == DECLARED_TYPE

    def test_signs_the_attachments_own_key(self, initiate):
        attachment, post = initiate()

        assert {"key": attachment.s3_key} in policy_of(post)["conditions"]

    def test_expires_in_five_minutes(self, initiate, presign_calls):
        initiate()

        assert presign_calls[0]["ExpiresIn"] == PRESIGN_EXPIRY_SECONDS
        assert PRESIGN_EXPIRY_SECONDS == 300

    def test_signs_against_the_configured_bucket(self, ctv, initiate,
                                                 presign_calls):
        attachment, _ = initiate()

        assert presign_calls[0]["Bucket"] == ctv.bucket_name
        assert presign_calls[0]["Key"] == attachment.s3_key

    def test_the_key_is_scoped_by_space_then_issue(self, ctv, issue, initiate):
        # An issue_id is only unique within its Space, so the Space has to be
        # above the Issue for a prefix-scoped policy or lifecycle rule to mean
        # what it says.
        attachment, _ = initiate()

        assert attachment.s3_key == (
            f"space/{ctv.space_id}/issue/{issue.issue_id}"
            f"/attachments/{attachment.attachment_id}")

    def test_the_name_is_not_in_the_key(self, initiate):
        attachment, _ = initiate(name="quarterly report.pdf")

        assert "quarterly" not in attachment.s3_key


class TestResignIssueAttachmentUpload:
    def test_returns_the_row_and_a_fresh_post(self, ctv, mgr, initiate):
        attachment, first = initiate()

        again, second = mgr.resign_issue_attachment_upload(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)

        assert again == attachment
        assert second["fields"]["key"] == first["fields"]["key"]

    def test_re_signs_the_rows_own_terms(self, ctv, mgr, initiate):
        attachment, _ = initiate(size=7, content_type="text/plain")

        _, post = mgr.resign_issue_attachment_upload(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)

        conditions = policy_of(post)["conditions"]
        assert ["content-length-range", 7, 7] in conditions
        assert {"Content-Type": "text/plain"} in conditions

    def test_leaves_the_row_alone(self, ctv, mgr, initiate):
        attachment, _ = initiate()

        mgr.resign_issue_attachment_upload(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)

        loaded, _ = mgr.get_issue_attachment(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)
        assert loaded == attachment

    def test_an_uploaded_attachment_has_nothing_left_to_sign(self, ctv, mgr,
                                                             attach):
        attachment = attach()

        with pytest.raises(DDBAttachmentStatusError, match="not PENDING"):
            mgr.resign_issue_attachment_upload(
                space_id=ctv.space_id, issue_id=attachment.issue_id,
                attachment_id=attachment.attachment_id)

    def test_a_missing_attachment_raises(self, ctv, mgr, issue):
        with pytest.raises(DDBMissingError):
            mgr.resign_issue_attachment_upload(space_id=ctv.space_id,
                                               issue_id=issue.issue_id,
                                               attachment_id="nosuch")

    def test_rejects_an_invalid_space_id(self, mgr, initiate):
        attachment, _ = initiate()

        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.resign_issue_attachment_upload(
                space_id="ENG#OPS", issue_id=attachment.issue_id,
                attachment_id=attachment.attachment_id)


class TestConfirmIssueAttachmentUploaded:
    def test_marks_it_uploaded(self, ctv, mgr, initiate, upload):
        attachment, _ = initiate()
        upload(attachment)

        confirmed = mgr.confirm_issue_attachment_uploaded(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)

        assert confirmed.status is AttachmentStatus.UPLOADED
        assert confirmed.version == attachment.version + 1

    def test_the_returned_object_matches_the_row(self, ctv, mgr, attach):
        # Assembled from what was written rather than read back, so this is
        # what keeps the two from drifting.
        confirmed = attach()

        loaded, _ = mgr.get_issue_attachment(
            space_id=ctv.space_id, issue_id=confirmed.issue_id,
            attachment_id=confirmed.attachment_id)
        assert loaded == confirmed

    def test_drops_the_expiry(self, ctv, attach, get_raw):
        # REMOVE, not a NULL: a confirmed attachment must not be reapable at
        # all, and DynamoDB's TTL reads a present attribute.
        confirmed = attach()

        row = attachment_row(get_raw, ctv, confirmed)
        assert "expires_at" not in row
        assert confirmed.expires_at is None

    def test_writes_s3s_size_and_content_type_not_the_callers(self, ctv, mgr,
                                                             initiate, upload):
        # The row records what landed in the bucket, not what was claimed.
        attachment, _ = initiate(size=DECLARED_SIZE,
                                 content_type="application/pdf")
        upload(attachment, body=b"a much longer body than declared",
               content_type="text/plain")

        confirmed = mgr.confirm_issue_attachment_uploaded(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)

        assert confirmed.size == len(b"a much longer body than declared")
        assert confirmed.size != DECLARED_SIZE
        assert confirmed.content_type == "text/plain"

    def test_counts_it_against_the_issue(self, ctv, mgr, issue, attach):
        attach()

        assert reload_issue(mgr, ctv, issue).num_attachments == 1

    def test_counts_it_against_the_comment_too(self, ctv, mgr, issue, comment,
                                               attach):
        attach(comment_id=comment.comment_id)

        assert reload_issue(mgr, ctv, issue).num_attachments == 1
        assert reload_comment(mgr, ctv, issue, comment).num_attachments == 1

    def test_an_unlinked_attachment_counts_on_no_comment(self, ctv, mgr, issue,
                                                         comment, attach):
        attach()

        assert reload_comment(mgr, ctv, issue, comment).num_attachments == 0

    def test_does_not_move_the_issue_version(self, ctv, mgr, issue, attach):
        # num_attachments is bookkeeping, not an edit to the Issue; see
        # issue_num_attachments_update.
        attach()

        assert reload_issue(mgr, ctv, issue).version == issue.version

    def test_does_not_move_the_comment_version(self, ctv, mgr, issue, comment,
                                               attach):
        attach(comment_id=comment.comment_id)

        assert reload_comment(mgr, ctv, issue, comment).version == \
            comment.version

    def test_issue_version_fencing_survives_attachment_writes(self, ctv, mgr,
                                                              issue, attach):
        attach()

        updated = mgr.update_issue(space_id=ctv.space_id,
                                   issue_id=issue.issue_id,
                                   title="new", description="new",
                                   version=issue.version)

        assert updated.title == "new"

    def test_comment_version_fencing_survives_attachment_writes(
            self, ctv, mgr, issue, comment, attach):
        attach(comment_id=comment.comment_id)

        updated = mgr.update_issue_comment(space_id=ctv.space_id,
                                           issue_id=issue.issue_id,
                                           comment_id=comment.comment_id,
                                           body="edited",
                                           version=comment.version)

        assert updated.body == "edited"

    def test_a_stale_issue_version_still_conflicts(self, ctv, mgr, issue,
                                                   attach):
        # The counter not moving the version must not be confused with the
        # version fence itself being weakened.
        attach()
        mgr.update_issue(space_id=ctv.space_id, issue_id=issue.issue_id,
                         title="one", description="one")

        with pytest.raises(DDBVersionConflictError):
            mgr.update_issue(space_id=ctv.space_id, issue_id=issue.issue_id,
                             title="two", description="two",
                             version=issue.version)

    def test_does_not_move_the_issue_updated_at(self, ctv, mgr, issue, attach):
        attach()

        assert reload_issue(mgr, ctv, issue).updated_at == issue.updated_at

    def test_confirming_twice_counts_once(self, ctv, mgr, issue, attach):
        confirmed = attach()

        with pytest.raises(DDBAttachmentStatusError):
            mgr.confirm_issue_attachment_uploaded(
                space_id=ctv.space_id, issue_id=confirmed.issue_id,
                attachment_id=confirmed.attachment_id)

        assert reload_issue(mgr, ctv, issue).num_attachments == 1

    def test_confirming_twice_counts_once_on_the_comment(self, ctv, mgr, issue,
                                                         comment, attach):
        confirmed = attach(comment_id=comment.comment_id)

        with pytest.raises(DDBAttachmentStatusError):
            mgr.confirm_issue_attachment_uploaded(
                space_id=ctv.space_id, issue_id=confirmed.issue_id,
                attachment_id=confirmed.attachment_id)

        assert reload_comment(mgr, ctv, issue, comment).num_attachments == 1

    def test_the_second_confirm_says_what_it_found(self, ctv, mgr, attach):
        confirmed = attach()

        with pytest.raises(DDBAttachmentStatusError, match="UPLOADED"):
            mgr.confirm_issue_attachment_uploaded(
                space_id=ctv.space_id, issue_id=confirmed.issue_id,
                attachment_id=confirmed.attachment_id)

    def test_the_second_confirm_leaves_the_row_alone(self, ctv, mgr, attach):
        confirmed = attach()

        with pytest.raises(DDBAttachmentStatusError):
            mgr.confirm_issue_attachment_uploaded(
                space_id=ctv.space_id, issue_id=confirmed.issue_id,
                attachment_id=confirmed.attachment_id)

        loaded, _ = mgr.get_issue_attachment(
            space_id=ctv.space_id, issue_id=confirmed.issue_id,
            attachment_id=confirmed.attachment_id)
        assert loaded == confirmed

    def test_a_missing_object_is_reported(self, ctv, mgr, initiate):
        # HeadObject on an absent key comes back as a bare 404, not NoSuchKey.
        attachment, _ = initiate()

        with pytest.raises(StorageObjectMissingError,
                           match="Attachment object not found"):
            mgr.confirm_issue_attachment_uploaded(
                space_id=ctv.space_id, issue_id=attachment.issue_id,
                attachment_id=attachment.attachment_id)

    def test_a_missing_object_moves_no_counter(self, ctv, mgr, issue, comment,
                                              initiate):
        attachment, _ = initiate(comment_id=comment.comment_id)

        with pytest.raises(StorageObjectMissingError):
            mgr.confirm_issue_attachment_uploaded(
                space_id=ctv.space_id, issue_id=attachment.issue_id,
                attachment_id=attachment.attachment_id)

        assert reload_issue(mgr, ctv, issue).num_attachments == 0
        assert reload_comment(mgr, ctv, issue, comment).num_attachments == 0

    def test_a_missing_object_leaves_the_row_pending(self, ctv, mgr, initiate,
                                                    upload):
        # So the caller can upload and confirm again without initiating a
        # second row.
        attachment, _ = initiate()

        with pytest.raises(StorageObjectMissingError):
            mgr.confirm_issue_attachment_uploaded(
                space_id=ctv.space_id, issue_id=attachment.issue_id,
                attachment_id=attachment.attachment_id)

        upload(attachment)
        confirmed = mgr.confirm_issue_attachment_uploaded(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)
        assert confirmed.status is AttachmentStatus.UPLOADED

    def test_a_missing_attachment_raises(self, ctv, mgr, issue):
        with pytest.raises(DDBMissingError):
            mgr.confirm_issue_attachment_uploaded(space_id=ctv.space_id,
                                                  issue_id=issue.issue_id,
                                                  attachment_id="nosuch")

    def test_a_deleted_issue_reports_the_issue(self, ctv, mgr, issue, initiate,
                                              upload):
        # The window between delete_issue and its sweep: the attachment row is
        # still there, the Issue holding num_attachments is not.
        attachment, _ = initiate()
        upload(attachment)
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

        with pytest.raises(DDBMissingError, match="Issue not found"):
            mgr.confirm_issue_attachment_uploaded(
                space_id=ctv.space_id, issue_id=attachment.issue_id,
                attachment_id=attachment.attachment_id)

    def test_a_deleted_comment_reports_the_comment(self, ctv, mgr, issue,
                                                   comment, initiate, upload):
        attachment, _ = initiate(comment_id=comment.comment_id)
        upload(attachment)
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        with pytest.raises(DDBMissingError, match="IssueComment not found"):
            mgr.confirm_issue_attachment_uploaded(
                space_id=ctv.space_id, issue_id=attachment.issue_id,
                attachment_id=attachment.attachment_id)

    def test_that_failure_rolls_the_whole_transaction_back(self, ctv, mgr,
                                                           issue, comment,
                                                           initiate, upload):
        attachment, _ = initiate(comment_id=comment.comment_id)
        upload(attachment)
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        with pytest.raises(DDBMissingError):
            mgr.confirm_issue_attachment_uploaded(
                space_id=ctv.space_id, issue_id=attachment.issue_id,
                attachment_id=attachment.attachment_id)

        loaded, _ = mgr.get_issue_attachment(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)
        assert loaded.status is AttachmentStatus.PENDING
        assert reload_issue(mgr, ctv, issue).num_attachments == 0

    def test_rejects_an_invalid_space_id(self, mgr, initiate):
        attachment, _ = initiate()

        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.confirm_issue_attachment_uploaded(
                space_id="ENG#OPS", issue_id=attachment.issue_id,
                attachment_id=attachment.attachment_id)


class TestGetIssueAttachment:
    def test_returns_the_row_and_a_download_url(self, ctv, mgr, attach):
        confirmed = attach()

        loaded, url = mgr.get_issue_attachment(
            space_id=ctv.space_id, issue_id=confirmed.issue_id,
            attachment_id=confirmed.attachment_id)

        assert loaded == confirmed
        assert confirmed.s3_key in url

    def test_the_url_carries_the_filename(self, ctv, mgr, attach):
        # How the human-readable name reaches the downloader without ever
        # entering the S3 key.
        confirmed = attach(name="quarterly report.pdf")

        _, url = mgr.get_issue_attachment(
            space_id=ctv.space_id, issue_id=confirmed.issue_id,
            attachment_id=confirmed.attachment_id)

        assert "response-content-disposition" in url.lower()
        assert "quarterly" in url

    def test_the_url_is_signed(self, ctv, mgr, attach):
        confirmed = attach()

        _, url = mgr.get_issue_attachment(
            space_id=ctv.space_id, issue_id=confirmed.issue_id,
            attachment_id=confirmed.attachment_id)

        assert "Signature=" in url or "X-Amz-Signature=" in url

    def test_a_pending_attachment_comes_back_without_a_url(self, ctv, mgr,
                                                           initiate):
        # A stuck upload must be visible so a caller can finish or delete it.
        attachment, _ = initiate()

        loaded, url = mgr.get_issue_attachment(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)

        assert loaded == attachment
        assert url is None

    def test_a_missing_attachment_raises(self, ctv, mgr, issue):
        with pytest.raises(DDBMissingError):
            mgr.get_issue_attachment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     attachment_id="nosuch")

    def test_rejects_an_invalid_space_id(self, mgr, attach):
        confirmed = attach()

        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.get_issue_attachment(
                space_id="ENG#OPS", issue_id=confirmed.issue_id,
                attachment_id=confirmed.attachment_id)


class TestGetIssueAttachments:
    def test_returns_them_oldest_first(self, ctv, mgr, issue, initiate):
        first, _ = initiate(name="a.pdf")
        second, _ = initiate(name="b.pdf")

        page, cursor = mgr.get_issue_attachments(space_id=ctv.space_id,
                                                 issue_id=issue.issue_id)

        assert [a.attachment_id for a in page] == [first.attachment_id,
                                                   second.attachment_id]
        assert cursor is None

    def test_can_be_read_newest_first(self, ctv, mgr, issue, initiate):
        first, _ = initiate(name="a.pdf")
        second, _ = initiate(name="b.pdf")

        page, _ = mgr.get_issue_attachments(space_id=ctv.space_id,
                                            issue_id=issue.issue_id,
                                            ascending=False)

        assert [a.attachment_id for a in page] == [second.attachment_id,
                                                   first.attachment_id]

    def test_includes_linked_and_unlinked(self, ctv, mgr, issue, comment,
                                          initiate):
        unlinked, _ = initiate(name="a.pdf")
        linked, _ = initiate(name="b.pdf", comment_id=comment.comment_id)

        page, _ = mgr.get_issue_attachments(space_id=ctv.space_id,
                                            issue_id=issue.issue_id)

        assert {a.attachment_id for a in page} == {unlinked.attachment_id,
                                                   linked.attachment_id}

    def test_includes_pending_ones(self, ctv, mgr, issue, initiate):
        attachment, _ = initiate()

        page, _ = mgr.get_issue_attachments(space_id=ctv.space_id,
                                            issue_id=issue.issue_id)

        assert [a.status for a in page] == [AttachmentStatus.PENDING]
        assert page[0].attachment_id == attachment.attachment_id

    def test_an_issue_with_no_attachments_is_empty(self, ctv, mgr, issue):
        page, cursor = mgr.get_issue_attachments(space_id=ctv.space_id,
                                                 issue_id=issue.issue_id)

        assert page == []
        assert cursor is None

    def test_excludes_every_other_row_type(self, ctv, mgr, issue, comment,
                                           initiate):
        other = mgr.create_issue(space_id=ctv.space_id, title="other",
                                 description="d", status=IssueStatus.TODO,
                                 creator="tester")
        mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                              blocking_issue_id=issue.issue_id,
                              blocked_issue_space_id=ctv.space_id,
                              blocked_issue_id=other.issue_id)
        initiate()

        page, _ = mgr.get_issue_attachments(space_id=ctv.space_id,
                                            issue_id=issue.issue_id)

        assert [type(item) for item in page] == [IssueAttachment]

    def test_limit_counts_only_attachment_rows(self, ctv, mgr, issue, comment,
                                               initiate):
        # The prefix is a key condition rather than a filter, so a page of one
        # is an attachment, never the info or comment row spent against Limit.
        first, _ = initiate(name="a.pdf")
        initiate(name="b.pdf")

        page, cursor = mgr.get_issue_attachments(space_id=ctv.space_id,
                                                 issue_id=issue.issue_id,
                                                 limit=1)

        assert [a.attachment_id for a in page] == [first.attachment_id]
        assert cursor is not None

    def test_pages_through_with_a_cursor(self, ctv, mgr, issue, initiate):
        ids = [initiate(name=f"{n}.pdf")[0].attachment_id for n in "abc"]

        seen = [a.attachment_id
                for a in mgr.paginate(mgr.get_issue_attachments,
                                      space_id=ctv.space_id,
                                      issue_id=issue.issue_id, limit=1)]

        assert seen == ids

    def test_another_issues_attachments_are_separate(self, ctv, mgr, issue,
                                                     initiate):
        other = mgr.create_issue(space_id=ctv.space_id, title="other",
                                 description="d", status=IssueStatus.TODO,
                                 creator="tester")
        initiate()

        page, _ = mgr.get_issue_attachments(space_id=ctv.space_id,
                                            issue_id=other.issue_id)
        assert page == []

    def test_rejects_an_invalid_space_id(self, mgr, issue):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.get_issue_attachments(space_id="ENG#OPS",
                                      issue_id=issue.issue_id)


class TestDeleteIssueAttachment:
    def test_removes_an_uploaded_row(self, ctv, mgr, attach, get_raw):
        confirmed = attach()

        mgr.delete_issue_attachment(space_id=ctv.space_id,
                                    issue_id=confirmed.issue_id,
                                    attachment_id=confirmed.attachment_id)

        assert attachment_row(get_raw, ctv, confirmed) is None

    def test_uncounts_an_uploaded_one_from_both_parents(self, ctv, mgr, issue,
                                                        comment, attach):
        confirmed = attach(comment_id=comment.comment_id)

        mgr.delete_issue_attachment(space_id=ctv.space_id,
                                    issue_id=confirmed.issue_id,
                                    attachment_id=confirmed.attachment_id)

        assert reload_issue(mgr, ctv, issue).num_attachments == 0
        assert reload_comment(mgr, ctv, issue, comment).num_attachments == 0

    def test_does_not_move_the_parents_versions(self, ctv, mgr, issue, comment,
                                                attach):
        confirmed = attach(comment_id=comment.comment_id)

        mgr.delete_issue_attachment(space_id=ctv.space_id,
                                    issue_id=confirmed.issue_id,
                                    attachment_id=confirmed.attachment_id)

        assert reload_issue(mgr, ctv, issue).version == issue.version
        assert reload_comment(mgr, ctv, issue, comment).version == \
            comment.version

    def test_removes_a_pending_row(self, ctv, mgr, initiate, get_raw):
        attachment, _ = initiate()

        mgr.delete_issue_attachment(space_id=ctv.space_id,
                                    issue_id=attachment.issue_id,
                                    attachment_id=attachment.attachment_id)

        assert attachment_row(get_raw, ctv, attachment) is None

    def test_a_pending_row_does_not_decrement(self, ctv, mgr, issue, comment,
                                              initiate, attach):
        # It was never counted, so decrementing for it would drive the
        # counters below the number of attachments actually there.
        attach(comment_id=comment.comment_id)
        pending, _ = initiate(comment_id=comment.comment_id)

        mgr.delete_issue_attachment(space_id=ctv.space_id,
                                    issue_id=pending.issue_id,
                                    attachment_id=pending.attachment_id)

        assert reload_issue(mgr, ctv, issue).num_attachments == 1
        assert reload_comment(mgr, ctv, issue, comment).num_attachments == 1

    def test_leaves_other_attachments_alone(self, ctv, mgr, issue, attach):
        kept = attach(name="kept.pdf")
        doomed = attach(name="doomed.pdf")

        mgr.delete_issue_attachment(space_id=ctv.space_id,
                                    issue_id=doomed.issue_id,
                                    attachment_id=doomed.attachment_id)

        page, _ = mgr.get_issue_attachments(space_id=ctv.space_id,
                                            issue_id=issue.issue_id)
        assert [a.attachment_id for a in page] == [kept.attachment_id]
        assert reload_issue(mgr, ctv, issue).num_attachments == 1

    def test_does_not_touch_the_object(self, ctv, mgr, attach, object_keys):
        # The row goes first; handle_issue_attachment_deleted removes the bytes
        # off the back of that delete.
        confirmed = attach()

        mgr.delete_issue_attachment(space_id=ctv.space_id,
                                    issue_id=confirmed.issue_id,
                                    attachment_id=confirmed.attachment_id)

        assert object_keys() == [confirmed.s3_key]

    def test_a_missing_attachment_raises(self, ctv, mgr, issue):
        with pytest.raises(DDBMissingError):
            mgr.delete_issue_attachment(space_id=ctv.space_id,
                                        issue_id=issue.issue_id,
                                        attachment_id="nosuch")

    def test_deleting_twice_raises_the_second_time(self, ctv, mgr, attach):
        confirmed = attach()
        mgr.delete_issue_attachment(space_id=ctv.space_id,
                                    issue_id=confirmed.issue_id,
                                    attachment_id=confirmed.attachment_id)

        with pytest.raises(DDBMissingError):
            mgr.delete_issue_attachment(space_id=ctv.space_id,
                                        issue_id=confirmed.issue_id,
                                        attachment_id=confirmed.attachment_id)

    def test_a_failed_delete_leaves_the_counter_alone(self, ctv, mgr, issue,
                                                      attach):
        attach()

        with pytest.raises(DDBMissingError):
            mgr.delete_issue_attachment(space_id=ctv.space_id,
                                        issue_id=issue.issue_id,
                                        attachment_id="nosuch")

        assert reload_issue(mgr, ctv, issue).num_attachments == 1

    def test_rejects_an_invalid_space_id(self, mgr, attach):
        confirmed = attach()

        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.delete_issue_attachment(
                space_id="ENG#OPS", issue_id=confirmed.issue_id,
                attachment_id=confirmed.attachment_id)


class TestTtlShapedRemoval:
    """What DynamoDB's TTL does to a PENDING row, which is a plain delete with
    no condition and no counter update. It must be safe precisely because a
    PENDING attachment was never counted."""

    def test_the_row_goes(self, ctv, mgr, initiate, get_raw):
        attachment, _ = initiate()

        mgr.delete_row(attachment)

        assert attachment_row(get_raw, ctv, attachment) is None

    def test_no_counter_moves(self, ctv, mgr, issue, comment, initiate):
        attachment, _ = initiate(comment_id=comment.comment_id)

        mgr.delete_row(attachment)

        assert reload_issue(mgr, ctv, issue).num_attachments == 0
        assert reload_comment(mgr, ctv, issue, comment).num_attachments == 0

    def test_counted_attachments_are_untouched(self, ctv, mgr, issue, comment,
                                               initiate, attach):
        attach(comment_id=comment.comment_id)
        pending, _ = initiate(comment_id=comment.comment_id)

        mgr.delete_row(pending)

        assert reload_issue(mgr, ctv, issue).num_attachments == 1
        assert reload_comment(mgr, ctv, issue, comment).num_attachments == 1


class TestHandleIssueAttachmentDeleted:
    def test_deletes_the_object(self, ctv, mgr, issue, attach, object_keys):
        confirmed = attach()

        mgr.handle_issue_attachment_deleted(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            attachment_id=confirmed.attachment_id)

        assert object_keys() == []

    def test_recomputes_the_key_from_the_ids(self, ctv, mgr, issue, attach,
                                             object_keys):
        # The event names the attachment; where its bytes live is this
        # library's business, so no key rides in the payload.
        kept = attach(name="kept.pdf")
        doomed = attach(name="doomed.pdf")

        mgr.handle_issue_attachment_deleted(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            attachment_id=doomed.attachment_id)

        assert object_keys() == [kept.s3_key]

    def test_replay_is_a_no_op(self, ctv, mgr, issue, attach, object_keys):
        # S3 answers 204 whether the key was there or not, so idempotence needs
        # no condition arranging.
        confirmed = attach()

        for _ in range(2):
            mgr.handle_issue_attachment_deleted(
                space_id=ctv.space_id, issue_id=issue.issue_id,
                attachment_id=confirmed.attachment_id)

        assert object_keys() == []

    def test_an_upload_that_never_happened_is_a_no_op(self, ctv, mgr, issue,
                                                      initiate, object_keys):
        # The TTL path: a PENDING row reaped with no object behind it.
        attachment, _ = initiate()

        mgr.handle_issue_attachment_deleted(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            attachment_id=attachment.attachment_id)

        assert object_keys() == []

    def test_leaves_the_rows_alone(self, ctv, mgr, issue, attach,
                                  scan_issue_rows):
        # Rows are the other sweeps' business; this handler is only about bytes.
        attach()
        before = len(scan_issue_rows())

        mgr.handle_issue_attachment_deleted(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            attachment_id="nosuch")

        assert len(scan_issue_rows()) == before

    def test_rejects_an_invalid_space_id(self, mgr, issue):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.handle_issue_attachment_deleted(space_id="ENG#OPS",
                                                issue_id=issue.issue_id,
                                                attachment_id="nosuch")


class TestHandleIssueCommentDeleted:
    def test_sweeps_the_comments_attachments(self, ctv, mgr, issue, comment,
                                             attach):
        attach(comment_id=comment.comment_id)
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        mgr.handle_issue_comment_deleted(space_id=ctv.space_id,
                                         issue_id=issue.issue_id,
                                         comment_id=comment.comment_id)

        page, _ = mgr.get_issue_attachments(space_id=ctv.space_id,
                                            issue_id=issue.issue_id)
        assert page == []

    def test_sweeps_pending_ones_too(self, ctv, mgr, issue, comment, initiate):
        initiate(comment_id=comment.comment_id)
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        mgr.handle_issue_comment_deleted(space_id=ctv.space_id,
                                         issue_id=issue.issue_id,
                                         comment_id=comment.comment_id)

        page, _ = mgr.get_issue_attachments(space_id=ctv.space_id,
                                            issue_id=issue.issue_id)
        assert page == []

    def test_leaves_another_comments_attachments_alone(self, ctv, mgr, issue,
                                                       comment, attach):
        other = mgr.create_issue_comment(space_id=ctv.space_id,
                                         issue_id=issue.issue_id,
                                         body="other", creator="alice")
        kept = attach(name="kept.pdf", comment_id=other.comment_id)
        attach(name="doomed.pdf", comment_id=comment.comment_id)
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        mgr.handle_issue_comment_deleted(space_id=ctv.space_id,
                                         issue_id=issue.issue_id,
                                         comment_id=comment.comment_id)

        page, _ = mgr.get_issue_attachments(space_id=ctv.space_id,
                                            issue_id=issue.issue_id)
        assert [a.attachment_id for a in page] == [kept.attachment_id]

    def test_leaves_unlinked_attachments_alone(self, ctv, mgr, issue, comment,
                                               attach):
        kept = attach(name="kept.pdf")
        attach(name="doomed.pdf", comment_id=comment.comment_id)
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        mgr.handle_issue_comment_deleted(space_id=ctv.space_id,
                                         issue_id=issue.issue_id,
                                         comment_id=comment.comment_id)

        page, _ = mgr.get_issue_attachments(space_id=ctv.space_id,
                                            issue_id=issue.issue_id)
        assert [a.attachment_id for a in page] == [kept.attachment_id]

    def test_leaves_the_rest_of_the_partition_alone(self, ctv, mgr, issue,
                                                    comment, attach):
        # The comment was deleted, not the Issue: its info row, its other
        # comments and its blockers all stay.
        surviving = mgr.create_issue_comment(space_id=ctv.space_id,
                                             issue_id=issue.issue_id,
                                             body="survivor", creator="alice")
        attach(comment_id=comment.comment_id)
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        mgr.handle_issue_comment_deleted(space_id=ctv.space_id,
                                         issue_id=issue.issue_id,
                                         comment_id=comment.comment_id)

        page, _ = mgr.get_issue_comments(space_id=ctv.space_id,
                                         issue_id=issue.issue_id)
        assert [c.comment_id for c in page] == [surviving.comment_id]
        assert reload_issue(mgr, ctv, issue).num_comments == 1

    def test_uncounts_the_swept_attachments_from_the_issue(self, ctv, mgr,
                                                           issue, comment,
                                                           attach):
        # The comment's own counter is gone with it, but the Issue is still
        # there and would otherwise overcount its attachments forever.
        attach(name="kept.pdf")
        attach(name="doomed.pdf", comment_id=comment.comment_id)
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        mgr.handle_issue_comment_deleted(space_id=ctv.space_id,
                                         issue_id=issue.issue_id,
                                         comment_id=comment.comment_id)

        assert reload_issue(mgr, ctv, issue).num_attachments == 1

    def test_replay_is_a_no_op(self, ctv, mgr, issue, comment, attach):
        attach(name="kept.pdf")
        attach(name="doomed.pdf", comment_id=comment.comment_id)
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        for _ in range(2):
            mgr.handle_issue_comment_deleted(space_id=ctv.space_id,
                                             issue_id=issue.issue_id,
                                             comment_id=comment.comment_id)

        assert reload_issue(mgr, ctv, issue).num_attachments == 1

    def test_no_op_for_a_comment_that_had_nothing(self, ctv, mgr, issue,
                                                  comment, scan_issue_rows):
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)
        before = len(scan_issue_rows())

        mgr.handle_issue_comment_deleted(space_id=ctv.space_id,
                                         issue_id=issue.issue_id,
                                         comment_id=comment.comment_id)

        assert len(scan_issue_rows()) == before

    def test_no_op_for_a_missing_issue(self, ctv, mgr, scan_issue_rows):
        mgr.handle_issue_comment_deleted(
            space_id=ctv.space_id, issue_id="nosuch",
            comment_id="0199f3a1-0000-7000-8000-000000000001")

        assert scan_issue_rows() == []

    def test_does_not_touch_the_objects(self, ctv, mgr, issue, comment, attach,
                                        object_keys):
        # Each row's delete drives its own IssueAttachmentDeleted.
        confirmed = attach(comment_id=comment.comment_id)
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        mgr.handle_issue_comment_deleted(space_id=ctv.space_id,
                                         issue_id=issue.issue_id,
                                         comment_id=comment.comment_id)

        assert object_keys() == [confirmed.s3_key]

    def test_rejects_an_invalid_space_id(self, mgr, issue, comment):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.handle_issue_comment_deleted(space_id="ENG#OPS",
                                             issue_id=issue.issue_id,
                                             comment_id=comment.comment_id)


class TestHandleIssueDeletedWithAttachments:
    """The Issue delete sweep, which sees attachments among everything else.

    Without an IssueAttachment branch the sweep's else would refuse the row and
    raise, so every Issue holding an attachment would land in the DLQ with its
    comments and blockers un-swept. These cover the whole partition rather than
    the attachments alone for that reason.
    """

    @pytest.fixture
    def populated(self, ctv, mgr, issue, comment, initiate, attach):
        """An Issue holding one of everything it can hold."""
        blocked = mgr.create_issue(space_id=ctv.space_id, title="blocked",
                                   description="d", status=IssueStatus.TODO,
                                   creator="tester")
        mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                              blocking_issue_id=issue.issue_id,
                              blocked_issue_space_id=ctv.space_id,
                              blocked_issue_id=blocked.issue_id)
        blocker = mgr.create_issue(space_id=ctv.space_id, title="blocker",
                                   description="d", status=IssueStatus.TODO,
                                   creator="tester")
        mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                              blocking_issue_id=blocker.issue_id,
                              blocked_issue_space_id=ctv.space_id,
                              blocked_issue_id=issue.issue_id)

        return {
            "blocked": blocked,
            "blocker": blocker,
            "linked": attach(name="linked.pdf",
                             comment_id=comment.comment_id),
            "unlinked": attach(name="unlinked.pdf"),
            "pending": initiate(name="pending.pdf")[0],
        }

    def test_the_partition_is_emptied(self, ctv, mgr, issue, populated,
                                      scan_issue_rows):
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=issue.issue_id)

        pk = f"ISSUE#{ctv.space_id}#{issue.issue_id}"
        assert [row for row in scan_issue_rows() if row["PK"] == {"S": pk}] \
            == []

    def test_only_the_other_issues_survive(self, ctv, mgr, issue, populated,
                                           scan_issue_rows):
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=issue.issue_id)

        # The two other Issues' own info rows, and nothing else: their blocker
        # rows named this Issue, so both directions were swept.
        assert sorted(row["type"]["S"] for row in scan_issue_rows()) == \
            ["IssueInfo", "IssueInfo"]

    def test_the_attachment_rows_are_gone(self, ctv, mgr, issue, populated):
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=issue.issue_id)

        page, _ = mgr.get_issue_attachments(space_id=ctv.space_id,
                                            issue_id=issue.issue_id)
        assert page == []

    def test_the_blocked_issues_counter_is_still_decremented(self, ctv, mgr,
                                                             issue,
                                                             populated):
        # The attachment branch must not cost the sweep the rest of its work.
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=issue.issue_id)

        blocked = mgr.get_issue(space_id=ctv.space_id,
                                issue_id=populated["blocked"].issue_id)
        assert blocked.num_active_blockers == 0

    def test_replay_is_a_no_op(self, ctv, mgr, issue, populated,
                               scan_issue_rows):
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

        for _ in range(2):
            mgr.handle_issue_deleted(space_id=ctv.space_id,
                                     issue_id=issue.issue_id)

        assert sorted(row["type"]["S"] for row in scan_issue_rows()) == \
            ["IssueInfo", "IssueInfo"]

    def test_a_linked_attachment_outliving_its_comment_is_still_swept(
            self, ctv, mgr, issue, comment, attach, scan_issue_rows):
        # The usual case rather than the exotic one: the sweep reads the
        # partition in sort key order, so 500#COMMENT# rows go before
        # 600#ATTACHMENT# ones and the comment holding the counter is already
        # gone when the attachment is reached.
        attach(comment_id=comment.comment_id)
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=issue.issue_id)

        assert scan_issue_rows() == []

    def test_the_objects_are_left_to_their_own_events(self, ctv, mgr, issue,
                                                      populated, object_keys):
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=issue.issue_id)

        assert sorted(object_keys()) == sorted(
            [populated["linked"].s3_key, populated["unlinked"].s3_key])


class TestStorageIsOptional:
    """A manager with no S3 configuration, which is what every consumer that
    does not touch attachments gets; see BasePL8.__init__."""

    @pytest.fixture
    def storageless(self, ctv, dynamodb_client, logger):
        return BasePL8(dynamodb_client=dynamodb_client,
                       table_name=ctv.table_name, logger=logger)

    def test_the_other_entities_still_work(self, ctv, storageless, issue):
        loaded = storageless.get_issue(space_id=ctv.space_id,
                                       issue_id=issue.issue_id)

        assert loaded.issue_id == issue.issue_id

    def test_initiating_says_what_is_missing(self, ctv, storageless, issue):
        with pytest.raises(StorageInternalError, match="s3_client"):
            storageless.initiate_issue_attachment_upload(
                space_id=ctv.space_id, issue_id=issue.issue_id, name="x.pdf",
                content_type=DECLARED_TYPE, size=DECLARED_SIZE,
                creator="alice")

    def test_initiating_writes_no_row(self, storageless, issue, initiate,
                                      scan_issue_rows):
        with pytest.raises(StorageInternalError):
            storageless.initiate_issue_attachment_upload(
                space_id=issue.space_id, issue_id=issue.issue_id,
                name="x.pdf", content_type=DECLARED_TYPE, size=DECLARED_SIZE,
                creator="alice")

        # A manager with no bucket can never produce a usable attachment, so it
        # must not leave a PENDING row for the TTL either.
        assert len(scan_issue_rows()) == 1

    def test_confirming_refuses(self, ctv, storageless, initiate):
        attachment, _ = initiate()

        with pytest.raises(StorageInternalError, match="bucket_name"):
            storageless.confirm_issue_attachment_uploaded(
                space_id=ctv.space_id, issue_id=attachment.issue_id,
                attachment_id=attachment.attachment_id)

    def test_reading_an_uploaded_attachment_refuses(self, ctv, storageless,
                                                    attach):
        confirmed = attach()

        with pytest.raises(StorageInternalError):
            storageless.get_issue_attachment(
                space_id=ctv.space_id, issue_id=confirmed.issue_id,
                attachment_id=confirmed.attachment_id)

    def test_reading_a_pending_attachment_still_works(self, ctv, storageless,
                                                      initiate):
        # There is no URL to sign for a PENDING row, so nothing needs storage.
        attachment, _ = initiate()

        loaded, url = storageless.get_issue_attachment(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)

        assert loaded == attachment
        assert url is None

    def test_the_object_handler_refuses(self, ctv, storageless, issue):
        with pytest.raises(StorageInternalError):
            storageless.handle_issue_attachment_deleted(
                space_id=ctv.space_id, issue_id=issue.issue_id,
                attachment_id="nosuch")

    def test_listing_attachments_needs_no_storage(self, ctv, storageless,
                                                  issue, initiate):
        attachment, _ = initiate()

        page, _ = storageless.get_issue_attachments(space_id=ctv.space_id,
                                                    issue_id=issue.issue_id)

        assert [a.attachment_id for a in page] == [attachment.attachment_id]


class TestAttachmentIntegrity:
    def test_a_new_issue_counts_no_attachments(self, issue):
        assert issue.num_attachments == 0

    def test_a_new_comment_counts_no_attachments(self, comment):
        assert comment.num_attachments == 0

    def test_an_issue_with_attachments_can_still_be_deleted(self, ctv, mgr,
                                                            issue, attach):
        # The same rule as num_comments: an Issue's attachments go with it,
        # unlike a Space, which refuses to be deleted while it holds Issues.
        attach()

        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

        with pytest.raises(DDBMissingError):
            mgr.get_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

    def test_a_comment_with_attachments_can_still_be_deleted(self, ctv, mgr,
                                                             issue, comment,
                                                             attach):
        attach(comment_id=comment.comment_id)

        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        with pytest.raises(DDBMissingError):
            mgr.get_issue_comment(space_id=ctv.space_id,
                                  issue_id=issue.issue_id,
                                  comment_id=comment.comment_id)

    def test_deleting_an_issue_leaves_its_attachments_for_the_sweep(
            self, ctv, mgr, issue, attach, scan_issue_rows):
        attach()
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

        assert len(scan_issue_rows()) == 1

    def test_attachments_on_a_done_issue_are_allowed(self, ctv, mgr, issue,
                                                     attach):
        # DONE is terminal for status transitions, not for attaching evidence.
        mgr.transition_issue(space_id=ctv.space_id, issue_id=issue.issue_id,
                             status=IssueStatus.DONE)

        confirmed = attach(name="postmortem.pdf")

        assert confirmed.status is AttachmentStatus.UPLOADED

    def test_counters_track_several_attachments(self, ctv, mgr, issue, comment,
                                               attach):
        attach(name="a.pdf", comment_id=comment.comment_id)
        attach(name="b.pdf", comment_id=comment.comment_id)
        attach(name="c.pdf")

        assert reload_issue(mgr, ctv, issue).num_attachments == 3
        assert reload_comment(mgr, ctv, issue, comment).num_attachments == 2
