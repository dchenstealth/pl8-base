# SPDX-License-Identifier: MIT

import gzip

import pytest

from pl8_base.const import RETRY_ISSUE_ID_COLLISIONS
from pl8_base.errors import (
    DDBArgsError,
    DDBCorruptedError,
    DDBIdCollisionError,
    DDBMissingError,
    DDBStillBlockedError,
    DDBTerminalStatusError,
    DDBVersionConflictError,
)
from pl8_base.types import IssueStatus
from pl8_base.util import DEFAULT_ID_ALPHABET


@pytest.fixture
def new_issue(ctv, mgr):
    """A freshly created TODO Issue."""
    return mgr.create_issue(space_id=ctv.space_id,
                            title="test title",
                            description="test desc",
                            status=IssueStatus.TODO)


def info_keys(info):
    return (info.PK, info.SK, info.GSI1PK, info.GSI1SK)


class TestCreateIssue:
    def test_returns_issue_info(self, ctv, new_issue):
        assert new_issue.space_id == ctv.space_id
        assert new_issue.title == "test title"
        assert new_issue.description == "test desc"
        assert new_issue.status == IssueStatus.TODO
        assert new_issue.version == 1
        assert new_issue.num_active_blockers == 0

    def test_returned_object_matches_what_was_stored(self, ctv, mgr, new_issue):
        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id) == new_issue

    def test_writes_exactly_one_row(self, new_issue, scan_all):
        assert len(scan_all()) == 1

    def test_row_keys_are_exact(self, ctv, new_issue, get_raw):
        issue_id = new_issue.issue_id
        row = get_raw(f"ISSUE#{ctv.space_id}#{issue_id}", "100#INFO")

        assert row is not None
        assert row["PK"] == {"S": f"ISSUE#{ctv.space_id}#{issue_id}"}
        assert row["SK"] == {"S": "100#INFO"}
        assert row["GSI1PK"] == {"S": f"ISSUESPACESTATUS#{ctv.space_id}#TODO"}
        assert row["GSI1SK"] == {
            "S": f"STATUSUPDATED#{new_issue.status_updated_at}#ISSUE#{issue_id}",
        }

    def test_row_is_tagged_with_its_type(self, new_issue, get_raw):
        assert get_raw(new_issue.PK, new_issue.SK)["type"] == {"S": "IssueInfo"}

    def test_description_is_stored_compressed(self, new_issue, get_raw):
        stored = get_raw(new_issue.PK, new_issue.SK)["description"]

        assert "S" not in stored
        assert gzip.decompress(stored["B"]).decode() == "test desc"

    def test_timestamps_are_set(self, new_issue):
        assert new_issue.created_at
        assert new_issue.updated_at == new_issue.created_at
        assert new_issue.status_updated_at == new_issue.created_at

    def test_generates_a_default_length_id(self, new_issue):
        assert len(new_issue.issue_id) == 6
        assert set(new_issue.issue_id) <= set(DEFAULT_ID_ALPHABET)

    def test_ids_are_distinct_across_calls(self, ctv, mgr):
        ids = {mgr.create_issue(space_id=ctv.space_id, title="t",
                                description="d",
                                status=IssueStatus.TODO).issue_id
               for _ in range(10)}
        assert len(ids) == 10

    def test_same_id_in_two_spaces_is_independent(self, ctv, mgr, monkeypatch,
                                                  scan_all):
        monkeypatch.setattr("pl8_base.mixins.issue.gen_issue_id",
                            lambda **kwargs: "dupdup")

        first = mgr.create_issue(space_id=ctv.space_id, title="a",
                                 description="d", status=IssueStatus.TODO)
        second = mgr.create_issue(space_id=ctv.other_space_id, title="b",
                                  description="d", status=IssueStatus.TODO)

        assert first.issue_id == second.issue_id == "dupdup"
        assert first.PK != second.PK
        assert len(scan_all()) == 2

    def test_accepts_a_non_default_starting_status(self, ctv, mgr):
        issue = mgr.create_issue(space_id=ctv.space_id, title="t",
                                 description="d",
                                 status=IssueStatus.IN_PROGRESS)

        assert issue.status == IssueStatus.IN_PROGRESS
        assert issue.GSI1PK == f"ISSUESPACESTATUS#{ctv.space_id}#IN_PROGRESS"

    def test_retries_past_an_id_collision(self, ctv, mgr, monkeypatch, scan_all):
        # The first generated id already exists, so create_issue must generate
        # another rather than overwrite the existing Issue.
        taken = mgr.create_issue(space_id=ctv.space_id, title="original",
                                 description="original desc",
                                 status=IssueStatus.TODO)

        ids = iter([taken.issue_id, "fresh1"])
        monkeypatch.setattr("pl8_base.mixins.issue.gen_issue_id",
                            lambda **kwargs: next(ids))

        created = mgr.create_issue(space_id=ctv.space_id, title="second",
                                   description="second desc",
                                   status=IssueStatus.TODO)

        assert created.issue_id == "fresh1"
        assert len(scan_all()) == 2

        untouched = mgr.get_issue(space_id=ctv.space_id,
                                  issue_id=taken.issue_id)
        assert untouched.title == "original"
        assert untouched.version == 1

    def test_gives_up_after_repeated_collisions(self, ctv, mgr, monkeypatch,
                                                scan_all):
        taken = mgr.create_issue(space_id=ctv.space_id, title="original",
                                 description="original desc",
                                 status=IssueStatus.TODO)

        calls = []

        def always_collide(**kwargs):
            calls.append(1)
            return taken.issue_id

        monkeypatch.setattr("pl8_base.mixins.issue.gen_issue_id", always_collide)

        with pytest.raises(DDBIdCollisionError):
            mgr.create_issue(space_id=ctv.space_id, title="second",
                             description="second desc",
                             status=IssueStatus.TODO)

        assert len(calls) == RETRY_ISSUE_ID_COLLISIONS
        assert len(scan_all()) == 1

    def test_keyword_only(self, ctv, mgr):
        with pytest.raises(TypeError):
            mgr.create_issue(ctv.space_id, "t", "d", IssueStatus.TODO)


class TestGetIssue:
    def test_returns_the_issue(self, ctv, mgr, new_issue):
        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id) == new_issue

    def test_round_trips_the_compressed_description(self, ctv, mgr, new_issue):
        loaded = mgr.get_issue(space_id=ctv.space_id,
                               issue_id=new_issue.issue_id)
        assert loaded.description == "test desc"

    def test_missing_issue_raises(self, ctv, mgr):
        with pytest.raises(DDBMissingError):
            mgr.get_issue(space_id=ctv.space_id, issue_id="nope00")

    def test_wrong_space_raises(self, ctv, mgr, new_issue):
        with pytest.raises(DDBMissingError):
            mgr.get_issue(space_id=ctv.other_space_id,
                          issue_id=new_issue.issue_id)


class TestUpdateIssue:
    def test_changes_title_and_description(self, ctv, mgr, new_issue):
        mgr.update_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id,
                         title="new title", description="new desc")

        loaded = mgr.get_issue(space_id=ctv.space_id,
                               issue_id=new_issue.issue_id)
        assert loaded.title == "new title"
        assert loaded.description == "new desc"

    def test_bumps_version(self, ctv, mgr, new_issue):
        mgr.update_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id,
                         title="new title", description="new desc")

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id).version == 2

    def test_sets_updated_at_and_leaves_created_at(self, ctv, mgr, new_issue):
        mgr.update_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id,
                         title="new title", description="new desc")

        loaded = mgr.get_issue(space_id=ctv.space_id,
                               issue_id=new_issue.issue_id)
        assert loaded.created_at == new_issue.created_at
        assert loaded.updated_at >= new_issue.updated_at

    def test_leaves_status_alone(self, ctv, mgr, new_issue):
        mgr.update_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id,
                         title="new title", description="new desc")

        loaded = mgr.get_issue(space_id=ctv.space_id,
                               issue_id=new_issue.issue_id)
        assert loaded.status == IssueStatus.TODO
        assert loaded.status_updated_at == new_issue.status_updated_at

    def test_leaves_keys_alone(self, ctv, mgr, new_issue):
        mgr.update_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id,
                         title="new title", description="new desc")

        loaded = mgr.get_issue(space_id=ctv.space_id,
                               issue_id=new_issue.issue_id)
        assert info_keys(loaded) == info_keys(new_issue)

    def test_keeps_the_description_compressed(self, ctv, mgr, new_issue,
                                              get_raw):
        mgr.update_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id,
                         title="new title", description="new desc")

        stored = get_raw(new_issue.PK, new_issue.SK)["description"]
        assert "S" not in stored
        assert gzip.decompress(stored["B"]).decode() == "new desc"

    def test_matching_version_applies(self, ctv, mgr, new_issue):
        mgr.update_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id,
                         title="new title", description="new desc",
                         version=new_issue.version)

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id).title == "new title"

    def test_stale_version_is_rejected(self, ctv, mgr, new_issue):
        mgr.update_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id,
                         title="first", description="first desc")

        with pytest.raises(DDBVersionConflictError):
            mgr.update_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             title="second", description="second desc",
                             version=new_issue.version)

    def test_stale_version_leaves_the_row_untouched(self, ctv, mgr, new_issue):
        mgr.update_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id,
                         title="first", description="first desc")

        with pytest.raises(DDBVersionConflictError):
            mgr.update_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             title="second", description="second desc",
                             version=new_issue.version)

        loaded = mgr.get_issue(space_id=ctv.space_id,
                               issue_id=new_issue.issue_id)
        assert loaded.title == "first"
        assert loaded.version == 2

    def test_missing_issue_raises(self, ctv, mgr):
        with pytest.raises(DDBMissingError):
            mgr.update_issue(space_id=ctv.space_id, issue_id="nope00",
                             title="t", description="d")

    def test_missing_issue_raises_missing_not_version_conflict(self, ctv, mgr):
        # A caller passing a version against a deleted Issue needs to hear that
        # it is gone, not that their read was stale.
        with pytest.raises(DDBMissingError):
            mgr.update_issue(space_id=ctv.space_id, issue_id="nope00",
                             title="t", description="d", version=1)


class TestTransitionIssue:
    def test_changes_status(self, ctv, mgr, new_issue):
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.IN_PROGRESS)

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id).status == \
            IssueStatus.IN_PROGRESS

    def test_rewrites_the_gsi_keys(self, ctv, mgr, new_issue, get_raw):
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.IN_PROGRESS)

        row = get_raw(new_issue.PK, new_issue.SK)
        issue_id = new_issue.issue_id
        assert row["GSI1PK"] == {
            "S": f"ISSUESPACESTATUS#{ctv.space_id}#IN_PROGRESS",
        }
        assert row["GSI1SK"]["S"].endswith(f"#ISSUE#{issue_id}")
        assert row["GSI1SK"]["S"] != new_issue.GSI1SK

    def test_advances_status_updated_at(self, ctv, mgr, new_issue,
                                        frozen_clock):
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.IN_PROGRESS)

        loaded = mgr.get_issue(space_id=ctv.space_id,
                               issue_id=new_issue.issue_id)
        assert loaded.status_updated_at > new_issue.status_updated_at
        assert loaded.GSI1SK == \
            f"STATUSUPDATED#{loaded.status_updated_at}#ISSUE#{loaded.issue_id}"

    def test_leaves_created_at_and_primary_key_alone(self, ctv, mgr, new_issue):
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.IN_PROGRESS)

        loaded = mgr.get_issue(space_id=ctv.space_id,
                               issue_id=new_issue.issue_id)
        assert loaded.created_at == new_issue.created_at
        assert loaded.PK == new_issue.PK
        assert loaded.SK == new_issue.SK

    def test_bumps_version(self, ctv, mgr, new_issue):
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.IN_PROGRESS)

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id).version == 2

    @pytest.mark.parametrize("status", [
        IssueStatus.IN_PROGRESS,
        IssueStatus.BLOCKED,
        IssueStatus.DONE,
    ])
    def test_from_todo(self, ctv, mgr, new_issue, status):
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id, status=status)

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id).status == status

    def test_in_progress_to_done(self, ctv, mgr, new_issue):
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.IN_PROGRESS)
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.DONE)

        loaded = mgr.get_issue(space_id=ctv.space_id,
                               issue_id=new_issue.issue_id)
        assert loaded.status == IssueStatus.DONE
        assert loaded.is_done is True

    @pytest.mark.parametrize("status", [
        IssueStatus.TODO,
        IssueStatus.IN_PROGRESS,
        IssueStatus.BLOCKED,
    ])
    def test_done_is_terminal(self, ctv, mgr, new_issue, status):
        # entities.md: "An Issue MUST NOT be allowed to transition from DONE to
        # another status."
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.DONE)

        with pytest.raises(DDBTerminalStatusError):
            mgr.transition_issue(space_id=ctv.space_id,
                                 issue_id=new_issue.issue_id, status=status)

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id).status == \
            IssueStatus.DONE

    def test_done_to_done_is_allowed(self, ctv, mgr, new_issue):
        # Not a transition out of DONE, so the terminal rule does not apply.
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.DONE)
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.DONE)

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id).status == \
            IssueStatus.DONE

    def test_blocked_with_no_active_blockers_can_move(self, ctv, mgr,
                                                      new_issue):
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.BLOCKED)
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.IN_PROGRESS)

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id).status == \
            IssueStatus.IN_PROGRESS

    @pytest.mark.parametrize("status", [
        IssueStatus.TODO,
        IssueStatus.IN_PROGRESS,
        IssueStatus.DONE,
    ])
    def test_cannot_leave_blocked_with_active_blockers(self, ctv, mgr,
                                                       new_issue, status):
        # entities.md: "An Issue MUST NOT be transitioned out of BLOCKED while
        # it has active IssueBlockers"
        blocker = mgr.create_issue(space_id=ctv.space_id, title="blocker",
                                   description="d", status=IssueStatus.TODO)
        mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                              blocking_issue_id=blocker.issue_id,
                              blocked_issue_space_id=ctv.space_id,
                              blocked_issue_id=new_issue.issue_id)

        with pytest.raises(DDBStillBlockedError):
            mgr.transition_issue(space_id=ctv.space_id,
                                 issue_id=new_issue.issue_id, status=status)

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id).status == \
            IssueStatus.BLOCKED

    def test_can_move_to_blocked_regardless_of_counter(self, ctv, mgr,
                                                       new_issue):
        blocker = mgr.create_issue(space_id=ctv.space_id, title="blocker",
                                   description="d", status=IssueStatus.TODO)
        mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                              blocking_issue_id=blocker.issue_id,
                              blocked_issue_space_id=ctv.space_id,
                              blocked_issue_id=new_issue.issue_id)

        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.BLOCKED)

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id).status == \
            IssueStatus.BLOCKED

    def test_missing_issue_raises(self, ctv, mgr):
        with pytest.raises(DDBMissingError):
            mgr.transition_issue(space_id=ctv.space_id, issue_id="nope00",
                                 status=IssueStatus.DONE)


class TestDeleteIssue:
    def test_removes_the_issue(self, ctv, mgr, new_issue):
        mgr.delete_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id)

        with pytest.raises(DDBMissingError):
            mgr.get_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id)

    def test_removes_the_row(self, ctv, mgr, new_issue, get_raw):
        mgr.delete_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id)
        assert get_raw(new_issue.PK, new_issue.SK) is None

    def test_leaves_other_issues_alone(self, ctv, mgr, new_issue, scan_all):
        other = mgr.create_issue(space_id=ctv.space_id, title="other",
                                 description="d", status=IssueStatus.TODO)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id)

        assert len(scan_all()) == 1
        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=other.issue_id) == other

    def test_leaves_the_same_id_in_another_space_alone(self, ctv, mgr,
                                                       monkeypatch):
        monkeypatch.setattr("pl8_base.mixins.issue.gen_issue_id",
                            lambda **kwargs: "dupdup")
        mgr.create_issue(space_id=ctv.space_id, title="a", description="d",
                         status=IssueStatus.TODO)
        other = mgr.create_issue(space_id=ctv.other_space_id, title="b",
                                 description="d", status=IssueStatus.TODO)

        mgr.delete_issue(space_id=ctv.space_id, issue_id="dupdup")

        assert mgr.get_issue(space_id=ctv.other_space_id,
                             issue_id="dupdup") == other

    def test_missing_issue_raises(self, ctv, mgr):
        with pytest.raises(DDBMissingError):
            mgr.delete_issue(space_id=ctv.space_id, issue_id="nope00")

    def test_deleting_twice_raises_the_second_time(self, ctv, mgr, new_issue):
        mgr.delete_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id)

        with pytest.raises(DDBMissingError):
            mgr.delete_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id)

    def test_a_done_issue_can_be_deleted(self, ctv, mgr, new_issue):
        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id,
                             status=IssueStatus.DONE)
        mgr.delete_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id)

        with pytest.raises(DDBMissingError):
            mgr.get_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id)


# An Issue key composes ISSUE#{space_id}#{issue_id}, so an unvalidated
# space_id carrying a "#" aliases one Issue onto another (space_id, issue_id)
# pair. entities.md: a space id MUST be 1-64 characters of [A-Za-z0-9_-].
BAD_SPACE_IDS = ["ENG#OPS", "", "ENG ONE", "ENG/x", "ENG\n", "x" * 65, None,
                 123]


def issue_entry_points(mgr, space_id):
    """Every public IssueMixin call, bound to one space_id.

    Listed exhaustively rather than sampled: a method added later without
    validation should fail here rather than ship. Each entry is
    (name, callable).
    """
    return [
        ("create_issue", lambda: mgr.create_issue(
            space_id=space_id, title="t", description="d",
            status=IssueStatus.TODO)),
        ("get_issue", lambda: mgr.get_issue(space_id=space_id, issue_id="abc")),
        ("get_issues_by_status", lambda: mgr.get_issues_by_status(
            space_id=space_id, status=IssueStatus.TODO)),
        ("get_issue_blockers", lambda: mgr.get_issue_blockers(
            space_id=space_id, blocked_issue_id="abc")),
        ("get_issue_blocking", lambda: mgr.get_issue_blocking(
            space_id=space_id, blocking_issue_id="abc")),
        ("update_issue", lambda: mgr.update_issue(
            space_id=space_id, issue_id="abc", title="t", description="d")),
        ("transition_issue", lambda: mgr.transition_issue(
            space_id=space_id, issue_id="abc", status=IssueStatus.DONE)),
        ("delete_issue", lambda: mgr.delete_issue(
            space_id=space_id, issue_id="abc")),
        ("add_issue_blocker/blocking", lambda: mgr.add_issue_blocker(
            blocking_issue_space_id=space_id, blocking_issue_id="a",
            blocked_issue_space_id="OPS", blocked_issue_id="b")),
        ("add_issue_blocker/blocked", lambda: mgr.add_issue_blocker(
            blocking_issue_space_id="OPS", blocking_issue_id="a",
            blocked_issue_space_id=space_id, blocked_issue_id="b")),
        ("delete_issue_blocker/blocking", lambda: mgr.delete_issue_blocker(
            blocking_issue_space_id=space_id, blocking_issue_id="a",
            blocked_issue_space_id="OPS", blocked_issue_id="b")),
        ("delete_issue_blocker/blocked", lambda: mgr.delete_issue_blocker(
            blocking_issue_space_id="OPS", blocking_issue_id="a",
            blocked_issue_space_id=space_id, blocked_issue_id="b")),
        ("handle_issue_done", lambda: mgr.handle_issue_done(
            space_id=space_id, issue_id="abc")),
        ("handle_issue_deleted", lambda: mgr.handle_issue_deleted(
            space_id=space_id, issue_id="abc")),
        ("handle_issue_num_active_blockers_zeroed",
         lambda: mgr.handle_issue_num_active_blockers_zeroed(
             space_id=space_id, issue_id="abc")),
    ]


class TestSpaceIdValidation:
    def test_every_entry_point_rejects_a_bad_space_id(self, mgr, subtests):
        for name, call in issue_entry_points(mgr, "ENG#OPS"):
            with subtests.test(entry_point=name), \
                    pytest.raises(DDBArgsError):
                call()

    @pytest.mark.parametrize("space_id", BAD_SPACE_IDS)
    def test_create_issue_rejects_each_bad_form(self, mgr, space_id):
        with pytest.raises(DDBArgsError):
            mgr.create_issue(space_id=space_id, title="t", description="d",
                             status=IssueStatus.TODO)

    def test_nothing_is_written_for_a_bad_space_id(self, mgr, scan_all):
        for _, call in issue_entry_points(mgr, "ENG#OPS"):
            with pytest.raises(DDBArgsError):
                call()

        assert scan_all() == []

    def test_a_hash_cannot_alias_one_issue_onto_another(self, ctv, mgr,
                                                        scan_all):
        """The aliasing this validation exists to prevent.

        Without it, create_issue(space_id="ENG#OPS") wrote
        PK=ISSUE#ENG#OPS#<id>, which get_issue(space_id="ENG",
        issue_id="OPS#<id>") then read back as a different Issue. Rejecting
        the write is what makes the aliased read find nothing.
        """
        with pytest.raises(DDBArgsError):
            mgr.create_issue(space_id=f"{ctv.space_id}#{ctv.other_space_id}",
                             title="t", description="d",
                             status=IssueStatus.TODO)

        assert scan_all() == []

        with pytest.raises(DDBMissingError):
            mgr.get_issue(space_id=ctv.space_id,
                          issue_id=f"{ctv.other_space_id}#abc123")

    def test_valid_space_ids_still_work(self, mgr):
        for space_id in ["ENG", "a", "my-space_1", "x" * 64]:
            issue = mgr.create_issue(space_id=space_id, title="t",
                                     description="d", status=IssueStatus.TODO)
            assert mgr.get_issue(space_id=space_id,
                                 issue_id=issue.issue_id) == issue


class TestIssueStatusValidation:
    """A bad status used to be written straight through to the row and its
    GSI1PK. from_item does validate, so the row could then never be read
    back: a bad argument surfaced later as DDBCorruptedError."""

    @pytest.mark.parametrize("status", ["NOT_A_STATUS", "todo", "", None, 123])
    def test_create_issue_rejects_it(self, ctv, mgr, status):
        with pytest.raises(DDBArgsError, match="Invalid issue status"):
            mgr.create_issue(space_id=ctv.space_id, title="t",
                             description="d", status=status)

    def test_create_issue_writes_no_row_for_a_bad_status(self, ctv, mgr,
                                                         scan_all):
        with pytest.raises(DDBArgsError):
            mgr.create_issue(space_id=ctv.space_id, title="t",
                             description="d", status="NOT_A_STATUS")

        assert scan_all() == []

    @pytest.mark.parametrize("status", ["NOT_A_STATUS", "todo", "", None, 123])
    def test_transition_issue_rejects_it(self, ctv, mgr, new_issue, status):
        with pytest.raises(DDBArgsError, match="Invalid issue status"):
            mgr.transition_issue(space_id=ctv.space_id,
                                 issue_id=new_issue.issue_id, status=status)

    def test_transition_issue_leaves_the_row_untouched(self, ctv, mgr,
                                                       new_issue):
        with pytest.raises(DDBArgsError):
            mgr.transition_issue(space_id=ctv.space_id,
                                 issue_id=new_issue.issue_id,
                                 status="NOT_A_STATUS")

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id) == new_issue

    def test_get_issues_by_status_rejects_it(self, ctv, mgr):
        with pytest.raises(DDBArgsError, match="Invalid issue status"):
            mgr.get_issues_by_status(space_id=ctv.space_id,
                                     status="NOT_A_STATUS")

    def test_a_created_issue_is_always_readable_back(self, ctv, mgr):
        """The guarantee the validation buys: no write path can produce a row
        that get_issue then reports as corrupt."""
        for status in IssueStatus:
            issue = mgr.create_issue(space_id=ctv.space_id, title="t",
                                     description="d", status=status)
            got = mgr.get_issue(space_id=ctv.space_id,
                                issue_id=issue.issue_id)

            assert got.status is status
            assert isinstance(got.status, IssueStatus)

    def test_the_bare_string_form_is_accepted_and_stored_as_the_enum(
            self, ctv, mgr):
        issue = mgr.create_issue(space_id=ctv.space_id, title="t",
                                 description="d", status="IN_PROGRESS")

        assert issue.status is IssueStatus.IN_PROGRESS
        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=issue.issue_id).status \
            is IssueStatus.IN_PROGRESS

    def test_a_corrupt_status_still_reads_as_corrupt(self, ctv, mgr, new_issue,
                                                     dynamodb_client):
        """Validation fences the write path, not rows already in the table:
        a status corrupted out of band is still reported as corruption."""
        dynamodb_client.update_item(
            TableName=ctv.table_name,
            Key={"PK": {"S": new_issue.PK}, "SK": {"S": new_issue.SK}},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": {"S": "NOT_A_STATUS"}},
        )

        with pytest.raises(DDBCorruptedError):
            mgr.get_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id)


class TestTransitionIssueVersioning:
    """transition_issue fences on version like update_issue does; see
    _build_update on why consumer-facing writes take it and handle_* must
    not."""

    def test_matching_version_applies(self, ctv, mgr, new_issue):
        updated = mgr.transition_issue(space_id=ctv.space_id,
                                       issue_id=new_issue.issue_id,
                                       status=IssueStatus.IN_PROGRESS,
                                       version=new_issue.version)

        assert updated.status == IssueStatus.IN_PROGRESS
        assert updated.version == new_issue.version + 1

    def test_stale_version_is_rejected(self, ctv, mgr, new_issue):
        mgr.update_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id,
                         title="moved on", description="d")

        with pytest.raises(DDBVersionConflictError):
            mgr.transition_issue(space_id=ctv.space_id,
                                 issue_id=new_issue.issue_id,
                                 status=IssueStatus.IN_PROGRESS,
                                 version=new_issue.version)

    def test_stale_version_leaves_the_row_untouched(self, ctv, mgr, new_issue):
        mgr.update_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id,
                         title="moved on", description="d")
        before = mgr.get_issue(space_id=ctv.space_id,
                               issue_id=new_issue.issue_id)

        with pytest.raises(DDBVersionConflictError):
            mgr.transition_issue(space_id=ctv.space_id,
                                 issue_id=new_issue.issue_id,
                                 status=IssueStatus.IN_PROGRESS,
                                 version=new_issue.version)

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=new_issue.issue_id) == before

    def test_omitting_version_still_applies_unfenced(self, ctv, mgr,
                                                     new_issue):
        mgr.update_issue(space_id=ctv.space_id, issue_id=new_issue.issue_id,
                         title="moved on", description="d")
        updated = mgr.transition_issue(space_id=ctv.space_id,
                                       issue_id=new_issue.issue_id,
                                       status=IssueStatus.IN_PROGRESS)

        assert updated.status == IssueStatus.IN_PROGRESS

    def test_missing_issue_raises_missing_not_version_conflict(self, ctv, mgr):
        with pytest.raises(DDBMissingError):
            mgr.transition_issue(space_id=ctv.space_id, issue_id="nope",
                                 status=IssueStatus.DONE, version=1)

    def test_a_domain_error_still_classifies_under_a_matching_version(
            self, ctv, mgr, new_issue):
        """version is checked before classify, so a matching version must not
        mask the terminal-status rule."""
        done = mgr.transition_issue(space_id=ctv.space_id,
                                    issue_id=new_issue.issue_id,
                                    status=IssueStatus.DONE)

        with pytest.raises(DDBTerminalStatusError):
            mgr.transition_issue(space_id=ctv.space_id,
                                 issue_id=new_issue.issue_id,
                                 status=IssueStatus.TODO,
                                 version=done.version)
