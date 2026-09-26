# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

import gzip
import uuid

import pytest

from pl8_base.errors import (
    DDBArgsError,
    DDBExistsError,
    DDBMissingError,
    DDBVersionConflictError,
)
from pl8_base.types import IssueComment, IssueStatus
from pl8_base.util import isotime_from_uuid7

pytestmark = pytest.mark.usefixtures("spaces")


@pytest.fixture
def issue(ctv, mgr):
    """A freshly created TODO Issue to hang comments off."""
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
def add_comment(ctv, mgr, issue):
    def _add(body, *, creator="alice"):
        return mgr.create_issue_comment(space_id=ctv.space_id,
                                        issue_id=issue.issue_id,
                                        body=body, creator=creator)

    return _add


@pytest.fixture
def initiate_attachment(ctv, mgr, issue):
    """Start an attachment upload on this Issue, leaving a PENDING row.

    Only the row matters to these tests: it is a 600#ATTACHMENT# row in the same
    partition as the comments, which is what the comment queries must not return.
    """
    def _initiate(*, name="report.pdf", comment_id=None):
        attachment, _ = mgr.initiate_issue_attachment_upload(
            space_id=ctv.space_id, issue_id=issue.issue_id, name=name,
            content_type="application/pdf", size=11, creator="alice",
            comment_id=comment_id)
        return attachment

    return _initiate


@pytest.fixture
def attach_file(ctv, mgr, s3_client, initiate_attachment):
    """A fully attached file: initiated, uploaded and confirmed.

    Arranged through the real methods, as everywhere else, which for an
    attachment means all three steps a caller takes.
    """
    def _attach(*, name="report.pdf", comment_id=None):
        attachment = initiate_attachment(name=name, comment_id=comment_id)
        s3_client.put_object(Bucket=ctv.bucket_name, Key=attachment.s3_key,
                             Body=b"hello world",
                             ContentType=attachment.content_type)
        return mgr.confirm_issue_attachment_uploaded(
            space_id=ctv.space_id, issue_id=attachment.issue_id,
            attachment_id=attachment.attachment_id)

    return _attach


def reload_issue(mgr, ctv, issue):
    return mgr.get_issue(space_id=ctv.space_id, issue_id=issue.issue_id)


class TestCreateIssueComment:
    def test_returns_the_comment(self, ctv, issue, comment):
        assert comment.space_id == ctv.space_id
        assert comment.issue_id == issue.issue_id
        assert comment.body == "first note"
        assert comment.creator == "alice"
        assert comment.version == 1

    def test_returned_object_matches_what_was_stored(self, ctv, mgr, issue,
                                                     comment):
        assert mgr.get_issue_comment(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            comment_id=comment.comment_id) == comment

    def test_generates_a_uuidv7_id(self, comment):
        assert uuid.UUID(comment.comment_id).version == 7

    def test_created_at_comes_from_the_id(self, comment):
        # The id is what orders the thread, so created_at is read back out of
        # it rather than taken from a second, independent clock reading.
        assert isotime_from_uuid7(comment.comment_id) == comment.created_at

    def test_writes_exactly_one_row(self, comment, issue, scan_issue_rows):
        # The Issue's own info row, plus the comment
        assert len(scan_issue_rows()) == 2

    def test_row_keys_are_exact(self, ctv, issue, comment, get_raw):
        row = get_raw(f"ISSUE#{ctv.space_id}#{issue.issue_id}",
                      f"500#COMMENT#{comment.comment_id}")

        assert row is not None
        assert row["PK"] == {"S": f"ISSUE#{ctv.space_id}#{issue.issue_id}"}
        assert row["SK"] == {"S": f"500#COMMENT#{comment.comment_id}"}

    def test_carries_no_gsi_keys(self, ctv, issue, comment, get_raw):
        # A comment is reachable from its Issue's partition alone, so it is
        # deliberately absent from GSI1.
        row = get_raw(f"ISSUE#{ctv.space_id}#{issue.issue_id}",
                      f"500#COMMENT#{comment.comment_id}")

        assert "GSI1PK" not in row
        assert "GSI1SK" not in row

    def test_sorts_between_the_info_row_and_the_blockers(self, comment):
        assert "100#INFO" < comment.SK < "800#BLOCKEDISSUE#"

    def test_stores_the_body_compressed(self, ctv, issue, comment, get_raw):
        row = get_raw(f"ISSUE#{ctv.space_id}#{issue.issue_id}",
                      f"500#COMMENT#{comment.comment_id}")

        assert gzip.decompress(row["body"]["B"]).decode() == "first note"

    def test_counts_against_the_issue(self, ctv, mgr, issue, add_comment):
        add_comment("one")
        add_comment("two")

        assert reload_issue(mgr, ctv, issue).num_comments == 2

    def test_does_not_move_the_issue_version(self, ctv, mgr, issue,
                                             add_comment):
        # num_comments is bookkeeping, not an edit to the Issue, so it must not
        # fail a concurrent version-fenced write; see
        # issue_num_comments_update.
        add_comment("one")

        assert reload_issue(mgr, ctv, issue).version == issue.version

    def test_issue_version_fencing_survives_comment_writes(self, ctv, mgr,
                                                           issue, add_comment):
        add_comment("one")

        updated = mgr.update_issue(space_id=ctv.space_id,
                                   issue_id=issue.issue_id,
                                   title="new", description="new",
                                   version=issue.version)

        assert updated.title == "new"

    def test_does_not_move_the_issue_updated_at(self, ctv, mgr, issue,
                                                add_comment):
        add_comment("one")

        assert reload_issue(mgr, ctv, issue).updated_at == issue.updated_at

    def test_a_missing_issue_is_rejected(self, ctv, mgr):
        with pytest.raises(DDBMissingError, match="Issue not found"):
            mgr.create_issue_comment(space_id=ctv.space_id,
                                     issue_id="nosuch",
                                     body="b", creator="alice")

    def test_a_rejected_comment_writes_nothing(self, ctv, mgr,
                                               scan_issue_rows):
        with pytest.raises(DDBMissingError):
            mgr.create_issue_comment(space_id=ctv.space_id,
                                     issue_id="nosuch",
                                     body="b", creator="alice")

        assert scan_issue_rows() == []

    def test_a_deleted_issue_takes_no_new_comments(self, ctv, mgr, issue):
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

        with pytest.raises(DDBMissingError):
            mgr.create_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     body="b", creator="alice")

    def test_an_id_clash_is_reported_not_rerolled(self, ctv, mgr, issue,
                                                  monkeypatch, comment):
        # Unlike an issue_id collision, a UUIDv7 clash is not contention to
        # retry past.
        monkeypatch.setattr("pl8_base.types.issue.uuid7",
                            lambda: uuid.UUID(comment.comment_id))

        with pytest.raises(DDBExistsError, match="IssueComment exists"):
            mgr.create_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     body="clash", creator="alice")

    def test_an_id_clash_leaves_the_counter_alone(self, ctv, mgr, issue,
                                                  monkeypatch, comment):
        monkeypatch.setattr("pl8_base.types.issue.uuid7",
                            lambda: uuid.UUID(comment.comment_id))

        with pytest.raises(DDBExistsError):
            mgr.create_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     body="clash", creator="alice")

        assert reload_issue(mgr, ctv, issue).num_comments == 1

    def test_allowed_on_a_done_issue(self, ctv, mgr, issue):
        # DONE is terminal for status transitions, not for discussion.
        mgr.transition_issue(space_id=ctv.space_id, issue_id=issue.issue_id,
                             status=IssueStatus.DONE)

        written = mgr.create_issue_comment(space_id=ctv.space_id,
                                           issue_id=issue.issue_id,
                                           body="postmortem",
                                           creator="alice")

        assert written.body == "postmortem"

    def test_rejects_an_invalid_space_id(self, mgr, issue):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.create_issue_comment(space_id="ENG#OPS",
                                     issue_id=issue.issue_id,
                                     body="b", creator="alice")

    def test_rejects_an_invalid_creator(self, ctv, mgr, issue):
        with pytest.raises(DDBArgsError, match="empty"):
            mgr.create_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     body="b", creator="")

    def test_rejects_a_non_string_body(self, ctv, mgr, issue):
        with pytest.raises(DDBArgsError, match="Compressed fields"):
            mgr.create_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     body=1, creator="alice")

    def test_keyword_only(self, ctv, mgr, issue):
        with pytest.raises(TypeError):
            mgr.create_issue_comment(ctv.space_id, issue.issue_id, "b", "alice")


class TestGetIssueComment:
    def test_returns_the_comment(self, ctv, mgr, issue, comment):
        loaded = mgr.get_issue_comment(space_id=ctv.space_id,
                                       issue_id=issue.issue_id,
                                       comment_id=comment.comment_id)

        assert loaded.body == "first note"
        assert loaded.creator == "alice"

    def test_a_missing_comment_raises(self, ctv, mgr, issue):
        with pytest.raises(DDBMissingError):
            mgr.get_issue_comment(space_id=ctv.space_id,
                                  issue_id=issue.issue_id,
                                  comment_id="nosuch")

    def test_rejects_an_invalid_space_id(self, mgr, issue, comment):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.get_issue_comment(space_id="ENG#OPS",
                                  issue_id=issue.issue_id,
                                  comment_id=comment.comment_id)


class TestGetIssueComments:
    def test_returns_them_oldest_first(self, ctv, mgr, issue, add_comment,
                                       frozen_clock):
        bodies = ["first", "second", "third"]
        for body in bodies:
            add_comment(body)

        page, cursor = mgr.get_issue_comments(space_id=ctv.space_id,
                                              issue_id=issue.issue_id)

        assert [c.body for c in page] == bodies
        assert cursor is None

    def test_ordering_survives_out_of_order_bodies(self, ctv, mgr, issue,
                                                   add_comment, frozen_clock):
        # Ordering comes from the id, so it tracks creation, not content.
        for body in ["zebra", "apple", "mango"]:
            add_comment(body)

        page, _ = mgr.get_issue_comments(space_id=ctv.space_id,
                                         issue_id=issue.issue_id)

        assert [c.body for c in page] == ["zebra", "apple", "mango"]

    def test_can_be_read_newest_first(self, ctv, mgr, issue, add_comment,
                                      frozen_clock):
        # Oldest first is the default, not the invariant it once was: a caller
        # showing the latest activity on a long thread pages from the end.
        for body in ["first", "second", "third"]:
            add_comment(body)

        page, _ = mgr.get_issue_comments(space_id=ctv.space_id,
                                        issue_id=issue.issue_id,
                                        ascending=False)

        assert [c.body for c in page] == ["third", "second", "first"]

    def test_newest_first_pages_from_the_end(self, ctv, mgr, issue,
                                             add_comment, frozen_clock):
        for body in ["first", "second", "third"]:
            add_comment(body)

        page, cursor = mgr.get_issue_comments(space_id=ctv.space_id,
                                             issue_id=issue.issue_id,
                                             limit=1, ascending=False)

        assert [c.body for c in page] == ["third"]
        assert cursor is not None

    def test_the_direction_does_not_change_the_set(self, ctv, mgr, issue,
                                                  add_comment, frozen_clock):
        for body in ["first", "second", "third"]:
            add_comment(body)

        ascending, _ = mgr.get_issue_comments(space_id=ctv.space_id,
                                             issue_id=issue.issue_id)
        descending, _ = mgr.get_issue_comments(space_id=ctv.space_id,
                                              issue_id=issue.issue_id,
                                              ascending=False)

        assert [c.comment_id for c in descending] == \
            [c.comment_id for c in reversed(ascending)]

    def test_an_issue_with_no_comments_is_empty(self, ctv, mgr, issue):
        page, cursor = mgr.get_issue_comments(space_id=ctv.space_id,
                                              issue_id=issue.issue_id)

        assert page == []
        assert cursor is None

    def test_excludes_the_issue_info_row(self, ctv, mgr, issue, comment):
        page, _ = mgr.get_issue_comments(space_id=ctv.space_id,
                                         issue_id=issue.issue_id)

        assert [type(item) for item in page] == [IssueComment]

    def test_excludes_blocker_rows(self, ctv, mgr, issue, comment):
        other = mgr.create_issue(space_id=ctv.space_id, title="other",
                                 description="d", status=IssueStatus.TODO,
                                 creator="tester")
        mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                              blocking_issue_id=issue.issue_id,
                              blocked_issue_space_id=ctv.space_id,
                              blocked_issue_id=other.issue_id)

        page, _ = mgr.get_issue_comments(space_id=ctv.space_id,
                                         issue_id=issue.issue_id)

        assert [c.comment_id for c in page] == [comment.comment_id]

    def test_limit_counts_only_comment_rows(self, ctv, mgr, issue,
                                            add_comment, frozen_clock):
        # The prefix is a key condition rather than a filter, so a page of one
        # is a comment, never the info row spent against Limit.
        add_comment("one")
        add_comment("two")

        page, cursor = mgr.get_issue_comments(space_id=ctv.space_id,
                                              issue_id=issue.issue_id,
                                              limit=1)

        assert [c.body for c in page] == ["one"]
        assert cursor is not None

    def test_pages_through_with_a_cursor(self, ctv, mgr, issue, add_comment,
                                         frozen_clock):
        bodies = ["one", "two", "three"]
        for body in bodies:
            add_comment(body)

        seen = [c.body for c in mgr.paginate(mgr.get_issue_comments,
                                             space_id=ctv.space_id,
                                             issue_id=issue.issue_id,
                                             limit=1)]

        assert seen == bodies

    def test_another_issues_comments_are_separate(self, ctv, mgr, issue,
                                                  comment):
        other = mgr.create_issue(space_id=ctv.space_id, title="other",
                                 description="d", status=IssueStatus.TODO,
                                 creator="tester")

        page, _ = mgr.get_issue_comments(space_id=ctv.space_id,
                                         issue_id=other.issue_id)

        assert page == []

    def test_rejects_an_invalid_space_id(self, mgr, issue):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.get_issue_comments(space_id="ENG#OPS",
                                   issue_id=issue.issue_id)


class TestGetIssueCommentsAfter:
    """The sort key range, whose upper bound is the interesting half: DynamoDB
    permits one sort key range condition, so the range has to bound itself
    inside the comment group rather than lean on a begins_with as well."""

    @pytest.fixture
    def thread(self, add_comment, frozen_clock):
        return [add_comment(body) for body in ("first", "second", "third")]

    def test_returns_only_what_came_after(self, ctv, mgr, issue, thread):
        page, cursor = mgr.get_issue_comments_after(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            last_comment_id=thread[0].comment_id)

        assert [c.body for c in page] == ["second", "third"]
        assert cursor is None

    def test_excludes_the_named_comment(self, ctv, mgr, issue, thread):
        page, _ = mgr.get_issue_comments_after(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            last_comment_id=thread[1].comment_id)

        assert thread[1].comment_id not in [c.comment_id for c in page]
        assert [c.body for c in page] == ["third"]

    def test_the_newest_comment_leaves_nothing(self, ctv, mgr, issue, thread):
        page, cursor = mgr.get_issue_comments_after(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            last_comment_id=thread[-1].comment_id)

        assert page == []
        assert cursor is None

    def test_without_a_last_comment_id_returns_the_thread(self, ctv, mgr,
                                                          issue, thread):
        page, _ = mgr.get_issue_comments_after(space_id=ctv.space_id,
                                               issue_id=issue.issue_id)

        assert [c.body for c in page] == ["first", "second", "third"]

    def test_returns_nothing_but_comments(self, ctv, mgr, issue, thread,
                                          initiate_attachment, attach_file):
        # The bound's whole purpose: an unbounded range would run past the
        # comments into the attachment and blocker rows and parse those.
        initiate_attachment()
        attach_file(comment_id=thread[0].comment_id)
        other = mgr.create_issue(space_id=ctv.space_id, title="other",
                                 description="d", status=IssueStatus.TODO,
                                 creator="tester")
        mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                              blocking_issue_id=issue.issue_id,
                              blocked_issue_space_id=ctv.space_id,
                              blocked_issue_id=other.issue_id)

        page, _ = mgr.get_issue_comments_after(
            space_id=ctv.space_id, issue_id=issue.issue_id,
            last_comment_id=thread[0].comment_id)

        assert {type(item) for item in page} == {IssueComment}
        assert [c.body for c in page] == ["second", "third"]

    def test_the_whole_thread_form_is_bounded_too(self, ctv, mgr, issue,
                                                  thread, initiate_attachment):
        initiate_attachment()

        page, _ = mgr.get_issue_comments_after(space_id=ctv.space_id,
                                               issue_id=issue.issue_id)

        assert {type(item) for item in page} == {IssueComment}

    def test_excludes_the_issue_info_row(self, ctv, mgr, issue, thread):
        page, _ = mgr.get_issue_comments_after(space_id=ctv.space_id,
                                               issue_id=issue.issue_id)

        assert len(page) == len(thread)

    def test_is_always_ascending(self, ctv, mgr, issue, thread):
        # "After" has one sensible order: the caller is extending a thread it
        # already holds, from where it stopped.
        page, _ = mgr.get_issue_comments_after(space_id=ctv.space_id,
                                               issue_id=issue.issue_id)

        assert [c.comment_id for c in page] == sorted(
            c.comment_id for c in thread)

    def test_pages_through_with_a_cursor(self, ctv, mgr, issue, thread):
        seen = [c.body for c in mgr.paginate(mgr.get_issue_comments_after,
                                             space_id=ctv.space_id,
                                             issue_id=issue.issue_id,
                                             last_comment_id=thread[0]
                                             .comment_id,
                                             limit=1)]

        assert seen == ["second", "third"]

    def test_an_issue_with_no_comments_is_empty(self, ctv, mgr, issue):
        page, cursor = mgr.get_issue_comments_after(space_id=ctv.space_id,
                                                    issue_id=issue.issue_id)

        assert page == []
        assert cursor is None

    def test_rejects_a_last_comment_id_that_is_not_a_uuidv7(self, ctv, mgr,
                                                            issue):
        # It composes a sort key bound, so it cannot be arbitrary text.
        with pytest.raises(DDBArgsError, match="Invalid UUID"):
            mgr.get_issue_comments_after(space_id=ctv.space_id,
                                         issue_id=issue.issue_id,
                                         last_comment_id="nosuch")

    def test_rejects_an_invalid_space_id(self, mgr, issue):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.get_issue_comments_after(space_id="ENG#OPS",
                                         issue_id=issue.issue_id)


class TestUpdateIssueComment:
    def test_replaces_the_body(self, ctv, mgr, issue, comment):
        updated = mgr.update_issue_comment(space_id=ctv.space_id,
                                           issue_id=issue.issue_id,
                                           comment_id=comment.comment_id,
                                           body="edited")

        assert updated.body == "edited"
        assert updated.version == 2

    def test_keeps_the_body_compressed(self, ctv, mgr, issue, comment,
                                       get_raw):
        mgr.update_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id,
                                 body="edited")

        row = get_raw(f"ISSUE#{ctv.space_id}#{issue.issue_id}",
                      f"500#COMMENT#{comment.comment_id}")
        assert gzip.decompress(row["body"]["B"]).decode() == "edited"

    def test_leaves_the_creator_alone(self, ctv, mgr, issue, comment):
        # A creator is fixed at creation; nothing here can reach it.
        updated = mgr.update_issue_comment(space_id=ctv.space_id,
                                           issue_id=issue.issue_id,
                                           comment_id=comment.comment_id,
                                           body="edited")

        assert updated.creator == "alice"

    def test_leaves_the_id_and_created_at_alone(self, ctv, mgr, issue,
                                                comment):
        updated = mgr.update_issue_comment(space_id=ctv.space_id,
                                           issue_id=issue.issue_id,
                                           comment_id=comment.comment_id,
                                           body="edited")

        assert updated.comment_id == comment.comment_id
        assert updated.created_at == comment.created_at
        assert updated.SK == comment.SK

    def test_sets_updated_at(self, ctv, mgr, issue, comment, frozen_clock):
        updated = mgr.update_issue_comment(space_id=ctv.space_id,
                                           issue_id=issue.issue_id,
                                           comment_id=comment.comment_id,
                                           body="edited")

        assert updated.updated_at > comment.updated_at

    def test_does_not_move_it_in_the_thread(self, ctv, mgr, issue,
                                            add_comment, frozen_clock):
        # Editing the oldest comment must not push it to the end.
        first = add_comment("first")
        add_comment("second")

        mgr.update_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=first.comment_id,
                                 body="edited")

        page, _ = mgr.get_issue_comments(space_id=ctv.space_id,
                                         issue_id=issue.issue_id)
        assert [c.body for c in page] == ["edited", "second"]

    def test_leaves_the_counter_alone(self, ctv, mgr, issue, comment):
        mgr.update_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id,
                                 body="edited")

        assert reload_issue(mgr, ctv, issue).num_comments == 1

    def test_a_missing_comment_raises(self, ctv, mgr, issue):
        with pytest.raises(DDBMissingError, match="IssueComment not found"):
            mgr.update_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     comment_id="nosuch", body="edited")

    def test_matching_version_applies(self, ctv, mgr, issue, comment):
        updated = mgr.update_issue_comment(space_id=ctv.space_id,
                                           issue_id=issue.issue_id,
                                           comment_id=comment.comment_id,
                                           body="edited",
                                           version=comment.version)

        assert updated.body == "edited"

    def test_stale_version_is_rejected(self, ctv, mgr, issue, comment):
        mgr.update_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id,
                                 body="first edit")

        with pytest.raises(DDBVersionConflictError):
            mgr.update_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     comment_id=comment.comment_id,
                                     body="second edit",
                                     version=comment.version)

    def test_rejects_an_invalid_space_id(self, mgr, issue, comment):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.update_issue_comment(space_id="ENG#OPS",
                                     issue_id=issue.issue_id,
                                     comment_id=comment.comment_id,
                                     body="edited")


class TestDeleteIssueComment:
    def test_removes_the_row(self, ctv, mgr, issue, comment, get_raw):
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        assert get_raw(f"ISSUE#{ctv.space_id}#{issue.issue_id}",
                       f"500#COMMENT#{comment.comment_id}") is None

    def test_uncounts_it_from_the_issue(self, ctv, mgr, issue, comment):
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        assert reload_issue(mgr, ctv, issue).num_comments == 0

    def test_does_not_move_the_issue_version(self, ctv, mgr, issue, comment):
        before = reload_issue(mgr, ctv, issue).version

        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        assert reload_issue(mgr, ctv, issue).version == before

    def test_leaves_the_issue_and_other_comments(self, ctv, mgr, issue,
                                                 add_comment, frozen_clock):
        first = add_comment("first")
        add_comment("second")

        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=first.comment_id)

        page, _ = mgr.get_issue_comments(space_id=ctv.space_id,
                                         issue_id=issue.issue_id)
        assert [c.body for c in page] == ["second"]
        assert reload_issue(mgr, ctv, issue).num_comments == 1

    def test_a_missing_comment_raises(self, ctv, mgr, issue):
        with pytest.raises(DDBMissingError, match="IssueComment not found"):
            mgr.delete_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     comment_id="nosuch")

    def test_deleting_twice_raises_the_second_time(self, ctv, mgr, issue,
                                                   comment):
        mgr.delete_issue_comment(space_id=ctv.space_id,
                                 issue_id=issue.issue_id,
                                 comment_id=comment.comment_id)

        with pytest.raises(DDBMissingError):
            mgr.delete_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     comment_id=comment.comment_id)

    def test_a_failed_delete_leaves_the_counter_alone(self, ctv, mgr, issue,
                                                      comment):
        with pytest.raises(DDBMissingError):
            mgr.delete_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     comment_id="nosuch")

        assert reload_issue(mgr, ctv, issue).num_comments == 1

    def test_rejects_an_invalid_space_id(self, mgr, issue, comment):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            mgr.delete_issue_comment(space_id="ENG#OPS",
                                     issue_id=issue.issue_id,
                                     comment_id=comment.comment_id)


class TestIssueCommentIntegrity:
    """num_comments does not gate deleting an Issue, unlike issue_count."""

    def test_a_new_issue_counts_no_comments(self, issue):
        assert issue.num_comments == 0

    def test_a_new_comment_counts_no_attachments(self, comment):
        # The comment-side counter AttachmentMixin maintains; a fresh comment
        # has nothing linked to it.
        assert comment.num_attachments == 0

    def test_an_issue_with_comments_can_still_be_deleted(self, ctv, mgr, issue,
                                                         add_comment):
        # The opposite of a Space, which refuses to be deleted while it holds
        # Issues. An Issue's comments go with it.
        add_comment("one")
        add_comment("two")

        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

        with pytest.raises(DDBMissingError):
            mgr.get_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

    def test_deleting_an_issue_leaves_its_comments_for_the_sweep(
            self, ctv, mgr, issue, add_comment, scan_issue_rows):
        # delete_issue removes the info row only; handle_issue_deleted is what
        # removes the comments.
        add_comment("one")
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

        assert len(scan_issue_rows()) == 1

    def test_deleting_a_comment_inside_the_sweep_window_reports_the_issue(
            self, ctv, mgr, issue, comment):
        # The Issue is gone but handle_issue_deleted has not run yet, so the
        # comment row is still there while the Issue holding num_comments is
        # not. The caller is told the Issue is missing, not the comment.
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

        with pytest.raises(DDBMissingError, match="Issue not found"):
            mgr.delete_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     comment_id=comment.comment_id)

    def test_that_failure_leaves_the_comment_for_the_sweep(
            self, ctv, mgr, issue, comment, scan_issue_rows):
        # The transaction rolls back, so the row must survive to be swept
        # rather than be half-deleted.
        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

        with pytest.raises(DDBMissingError):
            mgr.delete_issue_comment(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     comment_id=comment.comment_id)

        assert len(scan_issue_rows()) == 1

        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=issue.issue_id)
        assert scan_issue_rows() == []
