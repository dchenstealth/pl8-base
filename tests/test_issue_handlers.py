# SPDX-License-Identifier: MIT

from types import SimpleNamespace

import msgspec
import pytest

from botocore.exceptions import ClientError

from pl8_base.errors import DDBMissingError, DDBTransactionConflictError
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


def transaction_conflict():
    """The ClientError a cancelled-on-contention transaction raises."""
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException",
                      "Message": "Transaction cancelled"},
            "CancellationReasons": [{"Code": "TransactionConflict"}],
        },
        "TransactWriteItems",
    )


def reload(mgr, ctv, issue, *, space_id=None):
    return mgr.get_issue(space_id=space_id or ctv.space_id,
                         issue_id=issue.issue_id)


def blockers_of(mgr, ctv, issue, *, space_id=None):
    found, _ = mgr.get_issue_blockers(space_id=space_id or ctv.space_id,
                                      blocked_issue_id=issue.issue_id)
    return found


def blocking_from(mgr, ctv, issue, *, space_id=None):
    found, _ = mgr.get_issue_blocking(space_id=space_id or ctv.space_id,
                                      blocking_issue_id=issue.issue_id)
    return found


class TestHandleIssueDone:
    def test_marks_outbound_blockers_satisfied(self, ctv, mgr, make_issue,
                                               block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        rows = blocking_from(mgr, ctv, blocker)
        assert len(rows) == 1
        assert rows[0].is_blocking_issue_done is True

    def test_decrements_the_blocked_counter(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        assert reload(mgr, ctv, blocked).num_active_blockers == 0

    def test_does_not_itself_change_the_blocked_status(self, ctv, mgr,
                                                       make_issue, block):
        # Zeroing the counter is the trigger; the flip to TODO belongs to
        # handle_issue_num_active_blockers_zeroed.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        assert reload(mgr, ctv, blocked).status == IssueStatus.BLOCKED

    def test_handles_several_blocked_issues(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        blocked = [make_issue(f"blocked-{n}") for n in range(3)]
        for issue in blocked:
            block(blocker, issue)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        for issue in blocked:
            assert reload(mgr, ctv, issue).num_active_blockers == 0

    def test_pages_past_one_query_page(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        blocked = [make_issue(f"blocked-{n}") for n in range(60)]
        for issue in blocked:
            block(blocker, issue)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        for issue in blocked:
            assert reload(mgr, ctv, issue).num_active_blockers == 0

    def test_replay_is_a_no_op(self, ctv, mgr, make_issue, block):
        # Delivery is at-least-once: the second call must not decrement again.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        assert reload(mgr, ctv, blocked).num_active_blockers == 0

    def test_no_op_when_the_issue_is_not_done(self, ctv, mgr, make_issue,
                                              block):
        # A stale event, or one delivered ahead of the transition it describes.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        assert reload(mgr, ctv, blocked).num_active_blockers == 1
        assert blocking_from(mgr, ctv, blocker)[0].is_blocking_issue_done is False

    def test_no_op_without_outbound_blockers(self, ctv, mgr, make_issue,
                                             scan_all):
        issue = make_issue("lonely")
        mgr.transition_issue(space_id=ctv.space_id, issue_id=issue.issue_id,
                             status=IssueStatus.DONE)
        rows_before = len(scan_all())

        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=issue.issue_id)

        assert len(scan_all()) == rows_before

    def test_no_op_for_a_missing_issue(self, ctv, mgr, scan_all):
        rows_before = len(scan_all())

        mgr.handle_issue_done(space_id=ctv.space_id, issue_id="nope00")

        assert len(scan_all()) == rows_before

    def test_leaves_inbound_blockers_alone(self, ctv, mgr, make_issue, block):
        # Only the Issues this one blocks are affected, not the ones blocking it.
        subject = make_issue("subject")
        upstream = make_issue("upstream")
        block(upstream, subject)

        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=subject.issue_id)

        assert blockers_of(mgr, ctv, subject)[0].is_blocking_issue_done is False

    def test_works_across_spaces(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        remote = make_issue("remote", space_id=ctv.other_space_id)
        block(blocker, remote, blocked_space=ctv.other_space_id)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        assert mgr.get_issue(
            space_id=ctv.other_space_id,
            issue_id=remote.issue_id).num_active_blockers == 0


class TestHandleIssueNumActiveBlockersZeroed:
    def test_unblocks_the_issue(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)
        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)

        assert reload(mgr, ctv, blocked).status == IssueStatus.TODO

    def test_rewrites_the_gsi_keys(self, ctv, mgr, make_issue, block, get_raw):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)
        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)

        row = get_raw(f"ISSUE#{ctv.space_id}#{blocked.issue_id}", "100#INFO")
        assert row["GSI1PK"] == {"S": f"ISSUESPACESTATUS#{ctv.space_id}#TODO"}

    def test_no_op_when_blockers_remain(self, ctv, mgr, make_issue, block):
        # A late event, arriving after another blocker was added.
        blocked = make_issue("blocked")
        block(make_issue("one"), blocked)

        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)

        assert reload(mgr, ctv, blocked).status == IssueStatus.BLOCKED

    @pytest.mark.parametrize("status", [
        IssueStatus.TODO,
        IssueStatus.IN_PROGRESS,
        IssueStatus.DONE,
    ])
    def test_no_op_when_not_blocked(self, ctv, mgr, make_issue, status):
        issue = make_issue("issue", status=status)

        mgr.handle_issue_num_active_blockers_zeroed(space_id=ctv.space_id,
                                                    issue_id=issue.issue_id)

        assert reload(mgr, ctv, issue).status == status

    def test_does_not_resurrect_a_done_issue(self, ctv, mgr, make_issue):
        # The sharpest version of the stale-event case: DONE is terminal, so
        # a late unblock event must not move it.
        issue = make_issue("issue", status=IssueStatus.DONE)

        mgr.handle_issue_num_active_blockers_zeroed(space_id=ctv.space_id,
                                                    issue_id=issue.issue_id)

        assert reload(mgr, ctv, issue).status == IssueStatus.DONE

    def test_replay_is_a_no_op(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)
        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)
        after_first = reload(mgr, ctv, blocked)

        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)
        after_second = reload(mgr, ctv, blocked)

        assert after_second.status == IssueStatus.TODO
        assert after_second.version == after_first.version

    def test_no_op_for_a_missing_issue(self, ctv, mgr, scan_all):
        rows_before = len(scan_all())

        mgr.handle_issue_num_active_blockers_zeroed(space_id=ctv.space_id,
                                                    issue_id="nope00")

        assert len(scan_all()) == rows_before


class TestHandleIssueDeleted:
    def test_deletes_outbound_blockers(self, ctv, mgr, make_issue, block,
                                       get_raw):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=blocker.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=blocker.issue_id)

        pk = f"ISSUE#{ctv.space_id}#{blocker.issue_id}"
        sk = f"800#BLOCKEDISSUE#{ctv.space_id}#{blocked.issue_id}"
        assert get_raw(pk, sk) is None

    def test_decrements_counters_of_issues_it_blocked(self, ctv, mgr,
                                                      make_issue, block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=blocker.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=blocker.issue_id)

        assert reload(mgr, ctv, blocked).num_active_blockers == 0

    def test_satisfied_outbound_blockers_do_not_decrement(self, ctv, mgr,
                                                          make_issue, block):
        # Already decremented when the blocker went DONE.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        other = make_issue("other")
        block(blocker, blocked)
        block(other, blocked)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)
        assert reload(mgr, ctv, blocked).num_active_blockers == 1

        mgr.delete_issue(space_id=ctv.space_id, issue_id=blocker.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=blocker.issue_id)

        assert reload(mgr, ctv, blocked).num_active_blockers == 1
        assert [b.blocking_issue_id for b in blockers_of(mgr, ctv, blocked)] == \
            [other.issue_id]

    def test_deletes_inbound_blockers(self, ctv, mgr, make_issue, block,
                                      get_raw):
        # These rows live in the *other* Issue's partition, so they can only be
        # found through GSI1.
        subject = make_issue("subject")
        upstream = make_issue("upstream")
        block(upstream, subject)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=subject.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=subject.issue_id)

        pk = f"ISSUE#{ctv.space_id}#{upstream.issue_id}"
        sk = f"800#BLOCKEDISSUE#{ctv.space_id}#{subject.issue_id}"
        assert get_raw(pk, sk) is None

    def test_inbound_sweep_does_not_touch_counters(self, ctv, mgr, make_issue,
                                                   block):
        # The Issue holding num_active_blockers is the one being deleted.
        subject = make_issue("subject")
        upstream = make_issue("upstream")
        block(upstream, subject)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=subject.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=subject.issue_id)

        assert reload(mgr, ctv, upstream).num_active_blockers == 0

    def test_sweeps_both_directions(self, ctv, mgr, make_issue, block,
                                    scan_all):
        # entities.md: "When an Issue is deleted, every IssueBlocker naming it
        # MUST be deleted, whether it is the blocking or the blocked issue."
        subject = make_issue("subject")
        upstream = make_issue("upstream")
        downstream = make_issue("downstream")
        block(upstream, subject)
        block(subject, downstream)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=subject.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=subject.issue_id)

        remaining = [i for i in scan_all() if i["type"]["S"] == "IssueBlocker"]
        assert remaining == []
        assert reload(mgr, ctv, downstream).num_active_blockers == 0

    def test_leaves_unrelated_blockers_intact(self, ctv, mgr, make_issue,
                                              block):
        subject = make_issue("subject")
        blocked = make_issue("blocked")
        other = make_issue("other")
        elsewhere = make_issue("elsewhere")
        block(subject, blocked)
        block(other, elsewhere)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=subject.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=subject.issue_id)

        assert len(blockers_of(mgr, ctv, elsewhere)) == 1
        assert reload(mgr, ctv, elsewhere).num_active_blockers == 1

    def test_replay_is_a_no_op(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=blocker.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=blocker.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=blocker.issue_id)

        assert reload(mgr, ctv, blocked).num_active_blockers == 0

    def test_no_op_for_a_missing_issue(self, ctv, mgr, scan_all):
        rows_before = len(scan_all())

        mgr.handle_issue_deleted(space_id=ctv.space_id, issue_id="nope00")

        assert len(scan_all()) == rows_before

    def test_pages_past_one_query_page(self, ctv, mgr, make_issue, block,
                                       scan_all):
        subject = make_issue("subject")
        for n in range(60):
            block(subject, make_issue(f"blocked-{n}"))

        mgr.delete_issue(space_id=ctv.space_id, issue_id=subject.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=subject.issue_id)

        remaining = [i for i in scan_all() if i["type"]["S"] == "IssueBlocker"]
        assert remaining == []

    def test_works_across_spaces(self, ctv, mgr, make_issue, block, scan_all):
        subject = make_issue("subject")
        remote_up = make_issue("remote-up", space_id=ctv.other_space_id)
        remote_down = make_issue("remote-down", space_id=ctv.other_space_id)
        block(remote_up, subject, blocking_space=ctv.other_space_id)
        block(subject, remote_down, blocked_space=ctv.other_space_id)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=subject.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=subject.issue_id)

        remaining = [i for i in scan_all() if i["type"]["S"] == "IssueBlocker"]
        assert remaining == []
        assert mgr.get_issue(
            space_id=ctv.other_space_id,
            issue_id=remote_down.issue_id).num_active_blockers == 0


class TestTransactionConflicts:
    def test_cancelled_transaction_surfaces_as_a_conflict(self, ctv, mgr,
                                                          make_issue,
                                                          monkeypatch):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")

        def always_conflict(**kwargs):
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException",
                              "Message": "Transaction cancelled"},
                    "CancellationReasons": [{"Code": "TransactionConflict"}],
                },
                "TransactWriteItems",
            )

        monkeypatch.setattr(mgr.dynamodb_client, "transact_write_items",
                            always_conflict)

        with pytest.raises(DDBTransactionConflictError):
            mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                                  blocking_issue_id=blocker.issue_id,
                                  blocked_issue_space_id=ctv.space_id,
                                  blocked_issue_id=blocked.issue_id)

    def test_a_conflict_is_retried_and_then_succeeds(self, ctv, mgr, make_issue,
                                                     monkeypatch):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")

        real = mgr.dynamodb_client.transact_write_items
        calls = []

        def conflict_once(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ClientError(
                    {
                        "Error": {"Code": "TransactionCanceledException",
                                  "Message": "Transaction cancelled"},
                        "CancellationReasons": [{"Code": "TransactionConflict"}],
                    },
                    "TransactWriteItems",
                )
            return real(**kwargs)

        monkeypatch.setattr("pl8_base.util.time.sleep", lambda _: None)
        monkeypatch.setattr(mgr.dynamodb_client, "transact_write_items",
                            conflict_once)

        mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                              blocking_issue_id=blocker.issue_id,
                              blocked_issue_space_id=ctv.space_id,
                              blocked_issue_id=blocked.issue_id)

        assert len(calls) == 2
        assert reload(mgr, ctv, blocked).num_active_blockers == 1

    def test_handler_sweep_retries_a_conflict(self, ctv, mgr, make_issue,
                                              block, monkeypatch):
        # The sweep is driven by an SQS consumer, but a transient conflict
        # should not cost a redelivery: every blocking Issue reaching DONE at
        # once decrements the same counter, so conflicts are expected here.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)
        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)

        real = mgr.dynamodb_client.transact_write_items
        calls = []

        def conflict_once(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise transaction_conflict()
            return real(**kwargs)

        monkeypatch.setattr("pl8_base.util.time.sleep", lambda _: None)
        monkeypatch.setattr(mgr.dynamodb_client, "transact_write_items",
                            conflict_once)

        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        assert len(calls) == 2
        assert reload(mgr, ctv, blocked).num_active_blockers == 0

    def test_sweep_retry_is_per_transaction_not_per_sweep(self, ctv, mgr,
                                                          make_issue, block,
                                                          monkeypatch):
        # A conflict on the third blocker must retry that write, not restart
        # the sweep and redo the two already applied.
        blocker = make_issue("blocker")
        blocked = [make_issue(f"blocked-{n}") for n in range(4)]
        for issue in blocked:
            block(blocker, issue)
        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)

        real = mgr.dynamodb_client.transact_write_items
        calls = []

        def conflict_on_third(**kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise transaction_conflict()
            return real(**kwargs)

        monkeypatch.setattr("pl8_base.util.time.sleep", lambda _: None)
        monkeypatch.setattr(mgr.dynamodb_client, "transact_write_items",
                            conflict_on_third)

        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        # 4 blockers plus the one retried write, and no re-application of the
        # two that had already landed
        assert len(calls) == 5
        for issue in blocked:
            assert reload(mgr, ctv, issue).num_active_blockers == 0

    def test_deleted_sweep_retries_a_conflict(self, ctv, mgr, make_issue,
                                              block, monkeypatch):
        subject = make_issue("subject")
        blocked = make_issue("blocked")
        block(subject, blocked)
        mgr.delete_issue(space_id=ctv.space_id, issue_id=subject.issue_id)

        real = mgr.dynamodb_client.transact_write_items
        calls = []

        def conflict_once(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise transaction_conflict()
            return real(**kwargs)

        monkeypatch.setattr("pl8_base.util.time.sleep", lambda _: None)
        monkeypatch.setattr(mgr.dynamodb_client, "transact_write_items",
                            conflict_once)

        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=subject.issue_id)

        assert len(calls) == 2
        assert reload(mgr, ctv, blocked).num_active_blockers == 0

    def test_sweep_gives_up_after_repeated_conflicts(self, ctv, mgr,
                                                     make_issue, block,
                                                     monkeypatch):
        # Sustained contention is not transient; the consumer should see it and
        # let SQS redeliver rather than spin forever.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)
        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)

        def always_conflict(**kwargs):
            raise transaction_conflict()

        monkeypatch.setattr("pl8_base.util.time.sleep", lambda _: None)
        monkeypatch.setattr(mgr.dynamodb_client, "transact_write_items",
                            always_conflict)

        with pytest.raises(DDBTransactionConflictError):
            mgr.handle_issue_done(space_id=ctv.space_id,
                                  issue_id=blocker.issue_id)

    def test_single_item_handler_writes_are_not_transactions(self, ctv, mgr,
                                                             make_issue, block,
                                                             monkeypatch):
        # handle_issue_num_active_blockers_zeroed updates one item, so it
        # cannot raise TransactionConflict and needs no retry of its own.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)
        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        def fail_if_called(**kwargs):
            raise AssertionError("expected a plain UpdateItem")

        monkeypatch.setattr(mgr.dynamodb_client, "transact_write_items",
                            fail_if_called)

        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)

        assert reload(mgr, ctv, blocked).status == IssueStatus.TODO

    def test_a_condition_failure_is_not_retried(self, ctv, mgr, make_issue,
                                                monkeypatch):
        # Only TransactionConflict is transient; a failed domain condition
        # would fail identically on every retry.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked", status=IssueStatus.DONE)

        calls = []
        real = mgr.dynamodb_client.transact_write_items

        def counting(**kwargs):
            calls.append(1)
            return real(**kwargs)

        monkeypatch.setattr(mgr.dynamodb_client, "transact_write_items",
                            counting)

        with pytest.raises(Exception):
            mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                                  blocking_issue_id=blocker.issue_id,
                                  blocked_issue_space_id=ctv.space_id,
                                  blocked_issue_id=blocked.issue_id)

        assert len(calls) == 1


class TestUnblockingFlows:
    """The entities.md unblocking rules, driven end to end.

    The SQS consumer that would call the handlers does not exist yet, so the
    handlers are invoked by hand in the order the real path would deliver them.
    """

    def test_blocker_done_unblocks_the_issue(self, ctv, mgr, make_issue, block):
        # entities.md: "When a blocking Issue is transitioned to DONE and the
        # Issue it blocked has no other active blockers, the unblocked Issue
        # MUST be transitioned to TODO."
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        assert reload(mgr, ctv, blocked).status == IssueStatus.BLOCKED

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)
        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)

        loaded = reload(mgr, ctv, blocked)
        assert loaded.status == IssueStatus.TODO
        assert loaded.num_active_blockers == 0

    def test_stays_blocked_until_the_last_blocker_is_done(self, ctv, mgr,
                                                          make_issue, block):
        blocked = make_issue("blocked")
        first = make_issue("first")
        second = make_issue("second")
        block(first, blocked)
        block(second, blocked)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=first.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=first.issue_id)
        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)

        still = reload(mgr, ctv, blocked)
        assert still.status == IssueStatus.BLOCKED
        assert still.num_active_blockers == 1

        mgr.transition_issue(space_id=ctv.space_id, issue_id=second.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=second.issue_id)
        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)

        assert reload(mgr, ctv, blocked).status == IssueStatus.TODO

    def test_deleting_the_blocker_unblocks_the_issue(self, ctv, mgr, make_issue,
                                                     block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=blocker.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=blocker.issue_id)
        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)

        loaded = reload(mgr, ctv, blocked)
        assert loaded.status == IssueStatus.TODO
        assert loaded.num_active_blockers == 0

    def test_unblocked_issue_can_then_be_completed(self, ctv, mgr, make_issue,
                                                   block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)
        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocked.issue_id,
                             status=IssueStatus.DONE)

        assert reload(mgr, ctv, blocked).is_done is True

    def test_a_chain_unblocks_one_link_at_a_time(self, ctv, mgr, make_issue,
                                                 block):
        a = make_issue("a")
        b = make_issue("b")
        c = make_issue("c")
        block(a, b)
        block(b, c)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=a.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=a.issue_id)
        mgr.handle_issue_num_active_blockers_zeroed(space_id=ctv.space_id,
                                                    issue_id=b.issue_id)

        assert reload(mgr, ctv, b).status == IssueStatus.TODO
        assert reload(mgr, ctv, c).status == IssueStatus.BLOCKED

        mgr.transition_issue(space_id=ctv.space_id, issue_id=b.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=b.issue_id)
        mgr.handle_issue_num_active_blockers_zeroed(space_id=ctv.space_id,
                                                    issue_id=c.issue_id)

        assert reload(mgr, ctv, c).status == IssueStatus.TODO

    def test_a_cycle_stays_deadlocked(self, ctv, mgr, make_issue, block):
        # Neither can reach DONE while the other blocks it; entities.md calls
        # this out as permitted, with delete_issue_blocker as the remedy.
        a = make_issue("a")
        b = make_issue("b")
        block(a, b)
        block(b, a)

        for issue in (a, b):
            with pytest.raises(Exception):
                mgr.transition_issue(space_id=ctv.space_id,
                                     issue_id=issue.issue_id,
                                     status=IssueStatus.DONE)
            assert reload(mgr, ctv, issue).status == IssueStatus.BLOCKED

    def test_handlers_out_of_order_still_converge(self, ctv, mgr, make_issue,
                                                  block):
        # Delivery is unordered: the zeroed handler can arrive before the done
        # handler that would zero the counter. It must no-op, and the system
        # must still reach the right state once the done handler lands.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)
        assert reload(mgr, ctv, blocked).status == IssueStatus.BLOCKED

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)
        mgr.handle_issue_num_active_blockers_zeroed(
            space_id=ctv.space_id, issue_id=blocked.issue_id)

        assert reload(mgr, ctv, blocked).status == IssueStatus.TODO

    def test_deleting_a_blocked_issue_leaves_no_orphan_rows(self, ctv, mgr,
                                                            make_issue, block,
                                                            scan_all):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=blocked.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=blocked.issue_id)

        remaining = [i for i in scan_all() if i["type"]["S"] == "IssueBlocker"]
        assert remaining == []

        with pytest.raises(DDBMissingError):
            mgr.get_issue(space_id=ctv.space_id, issue_id=blocked.issue_id)


class TestSweepDoesNotTrustTheQueriedFlag:
    """handle_issue_deleted's phase-1 sweep reads is_blocking_issue_done from
    its query, then writes conditioned on it. handle_issue_done can flip that
    flag False -> True in between, and the whole point of the sweep is that no
    IssueBlocker outlives an Issue that names it (entities.md). So a failed
    condition must fall through to the plain delete rather than be read as
    "another writer already did this".
    """

    @pytest.fixture
    def satisfied_blocker(self, ctv, mgr, make_issue, block):
        """A blocker whose flag has since flipped, plus the stale row a sweep
        that queried before the flip would be holding."""
        blocking = make_issue("blocking")
        blocked = make_issue("blocked")
        blocker = block(blocking, blocked)

        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=blocking.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id,
                              issue_id=blocking.issue_id)

        stale = msgspec.structs.replace(blocker, is_blocking_issue_done=False)
        return SimpleNamespace(blocking=blocking, blocked=blocked, stale=stale)

    def test_the_row_is_deleted_anyway(self, ctv, mgr, satisfied_blocker,
                                       scan_all):
        mgr.delete_blocker_for_sweep(satisfied_blocker.stale)

        remaining = [i for i in scan_all() if i["type"]["S"] == "IssueBlocker"]
        assert remaining == []

    def test_the_counter_is_not_decremented_twice(self, ctv, mgr,
                                                  satisfied_blocker):
        before = reload(mgr, ctv, satisfied_blocker.blocked)
        assert before.num_active_blockers == 0

        mgr.delete_blocker_for_sweep(satisfied_blocker.stale)

        assert reload(mgr, ctv,
                      satisfied_blocker.blocked).num_active_blockers == 0

    def test_end_to_end_when_done_is_handled_before_deleted(
            self, ctv, mgr, make_issue, block, scan_all):
        """The delivery order that produces the stale read: IssueDone lands
        first, then IssueDeleted for the same Issue."""
        blocking = make_issue("blocking")
        blocked = make_issue("blocked")
        block(blocking, blocked)

        mgr.transition_issue(space_id=ctv.space_id,
                             issue_id=blocking.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id,
                              issue_id=blocking.issue_id)
        mgr.delete_issue(space_id=ctv.space_id, issue_id=blocking.issue_id)
        mgr.handle_issue_deleted(space_id=ctv.space_id,
                                 issue_id=blocking.issue_id)

        remaining = [i for i in scan_all() if i["type"]["S"] == "IssueBlocker"]
        assert remaining == []
        assert reload(mgr, ctv, blocked).num_active_blockers == 0

    def test_an_active_blocker_still_decrements(self, ctv, mgr, make_issue,
                                                block, scan_all):
        """The unchanged path: a flag that really is False still takes the
        transactional form, so the counter comes down with the row."""
        blocking = make_issue("blocking")
        blocked = make_issue("blocked")
        blocker = block(blocking, blocked)

        assert reload(mgr, ctv, blocked).num_active_blockers == 1

        mgr.delete_blocker_for_sweep(blocker)

        assert reload(mgr, ctv, blocked).num_active_blockers == 0
        assert [i for i in scan_all() if i["type"]["S"] == "IssueBlocker"] == []

    def test_a_blocker_whose_blocked_issue_is_gone_is_still_deleted(
            self, ctv, mgr, make_issue, block, scan_all):
        """The other way the transaction's condition fails: there is no
        counter left to decrement, and the row must still go."""
        blocking = make_issue("blocking")
        blocked = make_issue("blocked")
        blocker = block(blocking, blocked)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=blocked.issue_id)
        mgr.delete_blocker_for_sweep(blocker)

        assert [i for i in scan_all() if i["type"]["S"] == "IssueBlocker"] == []

    def test_a_replayed_sweep_is_still_a_no_op(self, ctv, mgr,
                                               satisfied_blocker, scan_all):
        """Falling through to the plain delete must not break idempotency:
        delete_blocker_row tolerates the row already being gone."""
        mgr.delete_blocker_for_sweep(satisfied_blocker.stale)
        mgr.delete_blocker_for_sweep(satisfied_blocker.stale)

        assert [i for i in scan_all() if i["type"]["S"] == "IssueBlocker"] == []
        assert reload(mgr, ctv,
                      satisfied_blocker.blocked).num_active_blockers == 0


class TestApplyIdempotentTransactionReportsOutcome:
    def test_returns_true_when_applied(self, ctv, mgr, make_issue):
        issue = make_issue("issue")
        applied = mgr.apply_idempotent_transaction([
            {"Update": mgr._build_update(
                PK=issue.PK, SK=issue.SK, title="moved")},
        ], "Error in test")

        assert applied is True
        assert reload(mgr, ctv, issue).title == "moved"

    def test_returns_false_when_a_condition_failed(self, ctv, mgr):
        applied = mgr.apply_idempotent_transaction([
            {"Update": mgr._build_update(
                PK=mgr.issue_pk(ctv.space_id, "nope"),
                SK="100#INFO", title="moved")},
        ], "Error in test")

        assert applied is False
