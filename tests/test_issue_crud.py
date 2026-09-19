import gzip

import pytest

from pl8_base.const import RETRY_ISSUE_ID_COLLISIONS
from pl8_base.errors import (
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
