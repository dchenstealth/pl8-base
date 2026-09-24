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
from pl8_base.util import comment_created_at

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
        assert comment_created_at(comment.comment_id) == comment.created_at

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
