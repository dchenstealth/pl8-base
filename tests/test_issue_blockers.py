# SPDX-License-Identifier: MIT

import pytest

from pl8_base.errors import (
    DDBArgsError,
    DDBBlockingIssueDoneError,
    DDBExistsError,
    DDBMissingError,
    DDBTerminalStatusError,
)
from pl8_base.types import IssueStatus


@pytest.fixture
def make_issue(ctv, mgr):
    def _make(title, *, status=IssueStatus.TODO, space_id=None):
        return mgr.create_issue(space_id=space_id or ctv.space_id,
                                title=title,
                                description=f"{title} desc",
                                status=status)

    return _make


@pytest.fixture
def block(ctv, mgr):
    def _block(blocking, blocked, *, blocking_space=None, blocked_space=None):
        return mgr.add_issue_blocker(
            blocking_issue_space_id=blocking_space or ctv.space_id,
            blocking_issue_id=blocking.issue_id,
            blocked_issue_space_id=blocked_space or ctv.space_id,
            blocked_issue_id=blocked.issue_id,
        )

    return _block


@pytest.fixture
def unblock(ctv, mgr):
    def _unblock(blocking, blocked, *, blocking_space=None, blocked_space=None):
        return mgr.delete_issue_blocker(
            blocking_issue_space_id=blocking_space or ctv.space_id,
            blocking_issue_id=blocking.issue_id,
            blocked_issue_space_id=blocked_space or ctv.space_id,
            blocked_issue_id=blocked.issue_id,
        )

    return _unblock


@pytest.fixture
def satisfy(ctv, mgr):
    """Drive a blocking Issue to DONE and run the handler that follows it."""
    def _satisfy(issue, *, space_id=None):
        space_id = space_id or ctv.space_id
        mgr.transition_issue(space_id=space_id, issue_id=issue.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=space_id, issue_id=issue.issue_id)

    return _satisfy


def reload(mgr, ctv, issue, *, space_id=None):
    return mgr.get_issue(space_id=space_id or ctv.space_id,
                         issue_id=issue.issue_id)


class TestAddIssueBlocker:
    def test_writes_the_row_with_exact_keys(self, ctv, mgr, make_issue, block,
                                            get_raw):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        pk = f"ISSUE#{ctv.space_id}#{blocker.issue_id}"
        sk = f"800#BLOCKEDISSUE#{ctv.space_id}#{blocked.issue_id}"
        row = get_raw(pk, sk)

        assert row is not None
        assert row["type"] == {"S": "IssueBlocker"}
        assert row["GSI1PK"] == {
            "S": f"BLOCKEDISSUE#{ctv.space_id}#{blocked.issue_id}",
        }
        assert row["GSI1SK"] == {
            "S": f"500#BLOCKINGISSUE#{ctv.space_id}#{blocker.issue_id}",
        }

    def test_row_lives_in_the_blocking_issues_partition(self, ctv, mgr,
                                                        make_issue, block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        assert reload(mgr, ctv, blocker).PK == \
            f"ISSUE#{ctv.space_id}#{blocker.issue_id}"

    def test_starts_unsatisfied(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        blockers, _ = mgr.get_issue_blockers(space_id=ctv.space_id,
                                             blocked_issue_id=blocked.issue_id)
        assert blockers[0].is_blocking_issue_done is False

    def test_blocks_the_blocked_issue(self, ctv, mgr, make_issue, block):
        # entities.md: "When an IssueBlocker is created, the blocked Issue
        # status MUST be transitioned to BLOCKED."
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        loaded = reload(mgr, ctv, blocked)
        assert loaded.status == IssueStatus.BLOCKED
        assert loaded.num_active_blockers == 1

    def test_blocks_an_in_progress_issue(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked", status=IssueStatus.IN_PROGRESS)
        block(blocker, blocked)

        assert reload(mgr, ctv, blocked).status == IssueStatus.BLOCKED

    def test_leaves_the_blocking_issue_alone(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        loaded = reload(mgr, ctv, blocker)
        assert loaded.status == IssueStatus.TODO
        assert loaded.num_active_blockers == 0

    def test_second_blocker_increments_the_counter(self, ctv, mgr, make_issue,
                                                   block):
        blocked = make_issue("blocked")
        block(make_issue("one"), blocked)
        block(make_issue("two"), blocked)

        loaded = reload(mgr, ctv, blocked)
        assert loaded.num_active_blockers == 2
        assert loaded.status == IssueStatus.BLOCKED

    def test_duplicate_is_rejected(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        with pytest.raises(DDBExistsError):
            block(blocker, blocked)

    def test_duplicate_does_not_move_the_counter(self, ctv, mgr, make_issue,
                                                 block, scan_all):
        # The whole point of doing this in a transaction: a rejected Put must
        # not leave an incremented counter behind.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        rows_before = len(scan_all())

        with pytest.raises(DDBExistsError):
            block(blocker, blocked)

        assert reload(mgr, ctv, blocked).num_active_blockers == 1
        assert len(scan_all()) == rows_before

    def test_done_blocking_issue_is_rejected(self, ctv, mgr, make_issue, block):
        # entities.md: "An IssueBlocker MUST NOT name an Issue with status DONE
        # as the blocking issue."
        blocker = make_issue("blocker", status=IssueStatus.DONE)
        blocked = make_issue("blocked")

        with pytest.raises(DDBBlockingIssueDoneError):
            block(blocker, blocked)

    def test_done_blocking_issue_writes_nothing(self, ctv, mgr, make_issue,
                                                block, scan_all):
        blocker = make_issue("blocker", status=IssueStatus.DONE)
        blocked = make_issue("blocked")
        rows_before = len(scan_all())

        with pytest.raises(DDBBlockingIssueDoneError):
            block(blocker, blocked)

        loaded = reload(mgr, ctv, blocked)
        assert loaded.status == IssueStatus.TODO
        assert loaded.num_active_blockers == 0
        assert len(scan_all()) == rows_before

    def test_done_blocked_issue_is_rejected(self, ctv, mgr, make_issue, block,
                                            scan_all):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked", status=IssueStatus.DONE)
        rows_before = len(scan_all())

        with pytest.raises(DDBTerminalStatusError):
            block(blocker, blocked)

        assert reload(mgr, ctv, blocked).status == IssueStatus.DONE
        assert len(scan_all()) == rows_before

    def test_missing_blocking_issue_is_rejected(self, ctv, mgr, make_issue,
                                                scan_all):
        blocked = make_issue("blocked")
        rows_before = len(scan_all())

        with pytest.raises(DDBMissingError):
            mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                                  blocking_issue_id="nope00",
                                  blocked_issue_space_id=ctv.space_id,
                                  blocked_issue_id=blocked.issue_id)

        assert reload(mgr, ctv, blocked).num_active_blockers == 0
        assert len(scan_all()) == rows_before

    def test_missing_blocked_issue_is_rejected(self, ctv, mgr, make_issue,
                                               scan_all):
        blocker = make_issue("blocker")
        rows_before = len(scan_all())

        with pytest.raises(DDBMissingError):
            mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                                  blocking_issue_id=blocker.issue_id,
                                  blocked_issue_space_id=ctv.space_id,
                                  blocked_issue_id="nope00")

        assert len(scan_all()) == rows_before

    def test_self_block_is_rejected(self, ctv, mgr, make_issue):
        # entities.md: "An IssueBlocker MUST NOT name the same Issue as both
        # the blocking and the blocked issue."
        issue = make_issue("issue")

        with pytest.raises(DDBArgsError, match="cannot block itself"):
            mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                                  blocking_issue_id=issue.issue_id,
                                  blocked_issue_space_id=ctv.space_id,
                                  blocked_issue_id=issue.issue_id)

    def test_self_block_check_does_not_touch_the_table(self, ctv, mgr,
                                                       make_issue, scan_all):
        issue = make_issue("issue")
        rows_before = len(scan_all())

        with pytest.raises(DDBArgsError):
            mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                                  blocking_issue_id=issue.issue_id,
                                  blocked_issue_space_id=ctv.space_id,
                                  blocked_issue_id=issue.issue_id)

        assert len(scan_all()) == rows_before
        assert reload(mgr, ctv, issue).status == IssueStatus.TODO

    def test_same_id_across_spaces_is_not_a_self_block(self, ctv, mgr,
                                                       monkeypatch):
        monkeypatch.setattr("pl8_base.mixins.issue.gen_issue_id",
                            lambda **kwargs: "dupdup")
        mgr.create_issue(space_id=ctv.space_id, title="a", description="d",
                         status=IssueStatus.TODO)
        mgr.create_issue(space_id=ctv.other_space_id, title="b",
                         description="d", status=IssueStatus.TODO)

        mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                              blocking_issue_id="dupdup",
                              blocked_issue_space_id=ctv.other_space_id,
                              blocked_issue_id="dupdup")

        assert mgr.get_issue(space_id=ctv.other_space_id,
                             issue_id="dupdup").status == IssueStatus.BLOCKED

    def test_cross_space_blocking(self, ctv, mgr, make_issue, block):
        # entities.md: "Issues MAY have blocking relationships across spaces."
        blocker = make_issue("blocker")
        remote = make_issue("remote", space_id=ctv.other_space_id)

        block(blocker, remote, blocked_space=ctv.other_space_id)

        loaded = mgr.get_issue(space_id=ctv.other_space_id,
                               issue_id=remote.issue_id)
        assert loaded.status == IssueStatus.BLOCKED
        assert loaded.num_active_blockers == 1

    def test_cycles_are_permitted(self, ctv, mgr, make_issue, block):
        # entities.md: cycles are legal and deadlocked; delete_issue_blocker is
        # the supported remedy.
        a = make_issue("a")
        b = make_issue("b")

        block(a, b)
        block(b, a)

        for issue in (a, b):
            loaded = reload(mgr, ctv, issue)
            assert loaded.status == IssueStatus.BLOCKED
            assert loaded.num_active_blockers == 1

    def test_keyword_only(self, ctv, mgr, make_issue):
        a = make_issue("a")
        b = make_issue("b")

        with pytest.raises(TypeError):
            mgr.add_issue_blocker(ctv.space_id, a.issue_id,
                                  ctv.space_id, b.issue_id)


class TestDeleteIssueBlocker:
    def test_removes_the_row(self, ctv, mgr, make_issue, block, unblock,
                             get_raw):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        unblock(blocker, blocked)

        pk = f"ISSUE#{ctv.space_id}#{blocker.issue_id}"
        sk = f"800#BLOCKEDISSUE#{ctv.space_id}#{blocked.issue_id}"
        assert get_raw(pk, sk) is None

    def test_decrements_the_counter(self, ctv, mgr, make_issue, block, unblock):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        unblock(blocker, blocked)

        assert reload(mgr, ctv, blocked).num_active_blockers == 0

    def test_does_not_itself_unblock_the_issue(self, ctv, mgr, make_issue,
                                               block, unblock):
        # Reaching zero is the trigger; the status flip is
        # handle_issue_num_active_blockers_zeroed's job.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        unblock(blocker, blocked)

        assert reload(mgr, ctv, blocked).status == IssueStatus.BLOCKED

    def test_leaves_other_blockers_in_place(self, ctv, mgr, make_issue, block,
                                            unblock):
        blocked = make_issue("blocked")
        one = make_issue("one")
        two = make_issue("two")
        block(one, blocked)
        block(two, blocked)

        unblock(one, blocked)

        loaded = reload(mgr, ctv, blocked)
        assert loaded.num_active_blockers == 1
        assert loaded.status == IssueStatus.BLOCKED

        remaining, _ = mgr.get_issue_blockers(
            space_id=ctv.space_id, blocked_issue_id=blocked.issue_id)
        assert [b.blocking_issue_id for b in remaining] == [two.issue_id]

    def test_satisfied_blocker_is_removed_without_decrementing(self, ctv, mgr,
                                                               make_issue,
                                                               block, unblock,
                                                               satisfy,
                                                               get_raw):
        # The counter was already decremented when the blocking Issue went
        # DONE, so deleting the row must not decrement it a second time.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)
        satisfy(blocker)

        assert reload(mgr, ctv, blocked).num_active_blockers == 0

        unblock(blocker, blocked)

        pk = f"ISSUE#{ctv.space_id}#{blocker.issue_id}"
        sk = f"800#BLOCKEDISSUE#{ctv.space_id}#{blocked.issue_id}"
        assert get_raw(pk, sk) is None
        assert reload(mgr, ctv, blocked).num_active_blockers == 0

    def test_satisfied_blocker_removal_with_another_active(self, ctv, mgr,
                                                           make_issue, block,
                                                           unblock, satisfy):
        blocked = make_issue("blocked")
        done_blocker = make_issue("done-blocker")
        live_blocker = make_issue("live-blocker")
        block(done_blocker, blocked)
        block(live_blocker, blocked)
        satisfy(done_blocker)

        assert reload(mgr, ctv, blocked).num_active_blockers == 1

        unblock(done_blocker, blocked)

        assert reload(mgr, ctv, blocked).num_active_blockers == 1

    def test_missing_pair_raises(self, ctv, mgr, make_issue):
        a = make_issue("a")
        b = make_issue("b")

        with pytest.raises(DDBMissingError):
            mgr.delete_issue_blocker(blocking_issue_space_id=ctv.space_id,
                                     blocking_issue_id=a.issue_id,
                                     blocked_issue_space_id=ctv.space_id,
                                     blocked_issue_id=b.issue_id)

    def test_deleting_twice_raises_the_second_time(self, ctv, mgr, make_issue,
                                                   block, unblock):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)
        unblock(blocker, blocked)

        with pytest.raises(DDBMissingError):
            unblock(blocker, blocked)

        assert reload(mgr, ctv, blocked).num_active_blockers == 0

    def test_counter_never_goes_negative(self, ctv, mgr, make_issue, block,
                                         unblock):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)
        unblock(blocker, blocked)

        with pytest.raises(DDBMissingError):
            unblock(blocker, blocked)

        assert reload(mgr, ctv, blocked).num_active_blockers >= 0

    def test_cross_space_blocker_can_be_deleted(self, ctv, mgr, make_issue,
                                                block, unblock):
        blocker = make_issue("blocker")
        remote = make_issue("remote", space_id=ctv.other_space_id)
        block(blocker, remote, blocked_space=ctv.other_space_id)

        unblock(blocker, remote, blocked_space=ctv.other_space_id)

        assert mgr.get_issue(space_id=ctv.other_space_id,
                             issue_id=remote.issue_id).num_active_blockers == 0

    def test_breaks_a_cycle(self, ctv, mgr, make_issue, block, unblock):
        # entities.md names this as the supported remedy for a deadlocked pair.
        a = make_issue("a")
        b = make_issue("b")
        block(a, b)
        block(b, a)

        unblock(a, b)

        assert reload(mgr, ctv, b).num_active_blockers == 0

        # b is now free to move, which lets a be unblocked in turn
        mgr.handle_issue_num_active_blockers_zeroed(space_id=ctv.space_id,
                                                    issue_id=b.issue_id)
        mgr.transition_issue(space_id=ctv.space_id, issue_id=b.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=b.issue_id)

        assert reload(mgr, ctv, a).num_active_blockers == 0
