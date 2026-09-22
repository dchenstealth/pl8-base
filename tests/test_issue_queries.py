# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

import pytest

from pl8_base.types import IssueStatus

pytestmark = pytest.mark.usefixtures("spaces")


@pytest.fixture
def make_issue(ctv, mgr):
    """Create an Issue in a space, defaulting to the primary one."""
    def _make(title, *, status=IssueStatus.TODO, space_id=None):
        return mgr.create_issue(space_id=space_id or ctv.space_id,
                                title=title,
                                description=f"{title} desc",
                                status=status)

    return _make


@pytest.fixture
def block(ctv, mgr):
    """Point one Issue at another as a blocker."""
    def _block(blocking, blocked, *, blocking_space=None, blocked_space=None):
        return mgr.add_issue_blocker(
            blocking_issue_space_id=blocking_space or ctv.space_id,
            blocking_issue_id=blocking.issue_id,
            blocked_issue_space_id=blocked_space or ctv.space_id,
            blocked_issue_id=blocked.issue_id,
        )

    return _block


def drain(query, **kwargs):
    """Page a cursor-based query to exhaustion, returning (items, pages)."""
    items = []
    pages = 0
    cursor = None

    while True:
        page, cursor = query(cursor=cursor, **kwargs)
        items.extend(page)
        pages += 1
        if cursor is None:
            return items, pages
        assert pages < 50, "query did not terminate"


class TestGetIssuesByStatus:
    def test_returns_matching_issues(self, ctv, mgr, make_issue):
        a = make_issue("a")
        b = make_issue("b")

        issues, _ = mgr.get_issues_by_status(space_id=ctv.space_id,
                                             status=IssueStatus.TODO)

        assert {i.issue_id for i in issues} == {a.issue_id, b.issue_id}

    def test_excludes_other_statuses(self, ctv, mgr, make_issue):
        todo = make_issue("todo")
        make_issue("wip", status=IssueStatus.IN_PROGRESS)

        issues, _ = mgr.get_issues_by_status(space_id=ctv.space_id,
                                             status=IssueStatus.TODO)

        assert [i.issue_id for i in issues] == [todo.issue_id]

    def test_excludes_other_spaces(self, ctv, mgr, make_issue):
        mine = make_issue("mine")
        make_issue("theirs", space_id=ctv.other_space_id)

        issues, _ = mgr.get_issues_by_status(space_id=ctv.space_id,
                                             status=IssueStatus.TODO)

        assert [i.issue_id for i in issues] == [mine.issue_id]

    def test_empty_result(self, ctv, mgr):
        issues, cursor = mgr.get_issues_by_status(space_id=ctv.space_id,
                                                  status=IssueStatus.DONE)

        assert issues == []
        assert cursor is None

    def test_returns_fully_populated_issues(self, ctv, mgr, make_issue):
        make_issue("a")

        issues, _ = mgr.get_issues_by_status(space_id=ctv.space_id,
                                             status=IssueStatus.TODO)

        # GSI1 projects ALL, so the description comes back decompressed too.
        assert issues[0].title == "a"
        assert issues[0].description == "a desc"

    def test_longest_in_status_first(self, ctv, mgr, make_issue, frozen_clock):
        first = make_issue("first")
        second = make_issue("second")
        third = make_issue("third")

        issues, _ = mgr.get_issues_by_status(space_id=ctv.space_id,
                                             status=IssueStatus.TODO)

        assert [i.issue_id for i in issues] == [
            first.issue_id, second.issue_id, third.issue_id,
        ]

    def test_a_transition_moves_an_issue_to_the_back(self, ctv, mgr, make_issue,
                                                     frozen_clock):
        # This is the property that makes status_updated_at the right sort key:
        # an Issue that just re-entered a status has sat in it the shortest.
        first = make_issue("first")
        second = make_issue("second")

        mgr.transition_issue(space_id=ctv.space_id, issue_id=first.issue_id,
                             status=IssueStatus.IN_PROGRESS)
        mgr.transition_issue(space_id=ctv.space_id, issue_id=first.issue_id,
                             status=IssueStatus.TODO)

        issues, _ = mgr.get_issues_by_status(space_id=ctv.space_id,
                                             status=IssueStatus.TODO)

        assert [i.issue_id for i in issues] == [second.issue_id, first.issue_id]

    def test_a_transition_removes_the_issue_from_the_old_status(self, ctv, mgr,
                                                                make_issue):
        issue = make_issue("a")
        mgr.transition_issue(space_id=ctv.space_id, issue_id=issue.issue_id,
                             status=IssueStatus.IN_PROGRESS)

        todo, _ = mgr.get_issues_by_status(space_id=ctv.space_id,
                                           status=IssueStatus.TODO)
        wip, _ = mgr.get_issues_by_status(space_id=ctv.space_id,
                                          status=IssueStatus.IN_PROGRESS)

        assert todo == []
        assert [i.issue_id for i in wip] == [issue.issue_id]

    def test_limit_is_honored(self, ctv, mgr, make_issue, frozen_clock):
        for n in range(5):
            make_issue(f"issue-{n}")

        issues, cursor = mgr.get_issues_by_status(space_id=ctv.space_id,
                                                  status=IssueStatus.TODO,
                                                  limit=2)

        assert len(issues) == 2
        assert cursor is not None
        assert isinstance(cursor, str)

    def test_no_cursor_on_the_last_page(self, ctv, mgr, make_issue):
        make_issue("only")

        issues, cursor = mgr.get_issues_by_status(space_id=ctv.space_id,
                                                  status=IssueStatus.TODO,
                                                  limit=2)

        assert len(issues) == 1
        assert cursor is None

    def test_cursor_continues_without_overlap(self, ctv, mgr, make_issue,
                                              frozen_clock):
        created = [make_issue(f"issue-{n}") for n in range(5)]

        first, cursor = mgr.get_issues_by_status(space_id=ctv.space_id,
                                                 status=IssueStatus.TODO,
                                                 limit=2)
        second, _ = mgr.get_issues_by_status(space_id=ctv.space_id,
                                             status=IssueStatus.TODO,
                                             limit=2, cursor=cursor)

        assert [i.issue_id for i in first] == [i.issue_id for i in created[:2]]
        assert [i.issue_id for i in second] == [i.issue_id for i in created[2:4]]

    def test_full_pagination_yields_each_issue_once(self, ctv, mgr, make_issue,
                                                    frozen_clock):
        created = [make_issue(f"issue-{n}") for n in range(7)]

        issues, pages = drain(mgr.get_issues_by_status,
                              space_id=ctv.space_id,
                              status=IssueStatus.TODO,
                              limit=2)

        assert [i.issue_id for i in issues] == [i.issue_id for i in created]
        assert pages == 4

    def test_ordering_survives_pagination(self, ctv, mgr, make_issue,
                                          frozen_clock):
        created = [make_issue(f"issue-{n}") for n in range(6)]

        issues, _ = drain(mgr.get_issues_by_status, space_id=ctv.space_id,
                          status=IssueStatus.TODO, limit=1)

        assert [i.issue_id for i in issues] == [i.issue_id for i in created]

    def test_default_limit(self, ctv, mgr, make_issue):
        make_issue("a")

        issues, cursor = mgr.get_issues_by_status(space_id=ctv.space_id,
                                                  status=IssueStatus.TODO)

        assert len(issues) == 1
        assert cursor is None


class TestGetIssueBlockers:
    def test_returns_the_blockers_of_an_issue(self, ctv, mgr, make_issue, block):
        blocked = make_issue("blocked")
        one = make_issue("one")
        two = make_issue("two")
        block(one, blocked)
        block(two, blocked)

        blockers, _ = mgr.get_issue_blockers(space_id=ctv.space_id,
                                             blocked_issue_id=blocked.issue_id)

        assert {b.blocking_issue_id for b in blockers} == {
            one.issue_id, two.issue_id,
        }
        assert all(b.blocked_issue_id == blocked.issue_id for b in blockers)

    def test_excludes_unrelated_blockers(self, ctv, mgr, make_issue, block):
        blocked = make_issue("blocked")
        other = make_issue("other")
        blocker = make_issue("blocker")
        block(blocker, blocked)
        block(blocker, other)

        blockers, _ = mgr.get_issue_blockers(space_id=ctv.space_id,
                                             blocked_issue_id=blocked.issue_id)

        assert len(blockers) == 1
        assert blockers[0].blocked_issue_id == blocked.issue_id

    def test_does_not_return_the_issues_own_outbound_blockers(self, ctv, mgr,
                                                              make_issue, block):
        # What blocks X, not what X blocks.
        x = make_issue("x")
        y = make_issue("y")
        block(x, y)

        blockers, _ = mgr.get_issue_blockers(space_id=ctv.space_id,
                                             blocked_issue_id=x.issue_id)

        assert blockers == []

    def test_includes_cross_space_blockers(self, ctv, mgr, make_issue, block):
        blocked = make_issue("blocked")
        remote = make_issue("remote", space_id=ctv.other_space_id)
        block(remote, blocked, blocking_space=ctv.other_space_id)

        blockers, _ = mgr.get_issue_blockers(space_id=ctv.space_id,
                                             blocked_issue_id=blocked.issue_id)

        assert len(blockers) == 1
        assert blockers[0].blocking_issue_space_id == ctv.other_space_id
        assert blockers[0].blocking_issue_id == remote.issue_id

    def test_includes_satisfied_blockers(self, ctv, mgr, make_issue, block):
        # The is_blocking_issue_done filter was deliberately removed so a page
        # always holds up to limit items; callers wanting only active blockers
        # read IssueInfo.num_active_blockers instead.
        blocked = make_issue("blocked")
        blocker = make_issue("blocker")
        block(blocker, blocked)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        blockers, _ = mgr.get_issue_blockers(space_id=ctv.space_id,
                                             blocked_issue_id=blocked.issue_id)

        assert len(blockers) == 1
        assert blockers[0].is_blocking_issue_done is True

    def test_empty_result(self, ctv, mgr, make_issue):
        issue = make_issue("lonely")

        blockers, cursor = mgr.get_issue_blockers(
            space_id=ctv.space_id, blocked_issue_id=issue.issue_id)

        assert blockers == []
        assert cursor is None

    def test_limit_and_cursor(self, ctv, mgr, make_issue, block):
        blocked = make_issue("blocked")
        for n in range(5):
            block(make_issue(f"blocker-{n}"), blocked)

        page, cursor = mgr.get_issue_blockers(
            space_id=ctv.space_id, blocked_issue_id=blocked.issue_id, limit=2)

        assert len(page) == 2
        assert cursor is not None

    def test_full_pagination_yields_each_blocker_once(self, ctv, mgr,
                                                      make_issue, block):
        blocked = make_issue("blocked")
        expected = {make_issue(f"blocker-{n}").issue_id for n in range(5)}
        for issue_id in expected:
            mgr.add_issue_blocker(blocking_issue_space_id=ctv.space_id,
                                  blocking_issue_id=issue_id,
                                  blocked_issue_space_id=ctv.space_id,
                                  blocked_issue_id=blocked.issue_id)

        blockers, pages = drain(mgr.get_issue_blockers,
                                space_id=ctv.space_id,
                                blocked_issue_id=blocked.issue_id,
                                limit=2)

        assert [b.blocking_issue_id for b in blockers] == \
            sorted({b.blocking_issue_id for b in blockers})
        assert {b.blocking_issue_id for b in blockers} == expected
        assert pages == 3


class TestGetIssueBlocking:
    def test_returns_what_an_issue_blocks(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        one = make_issue("one")
        two = make_issue("two")
        block(blocker, one)
        block(blocker, two)

        blocking, _ = mgr.get_issue_blocking(
            space_id=ctv.space_id, blocking_issue_id=blocker.issue_id)

        assert {b.blocked_issue_id for b in blocking} == {
            one.issue_id, two.issue_id,
        }
        assert all(b.blocking_issue_id == blocker.issue_id for b in blocking)

    def test_does_not_return_the_issue_info_row(self, ctv, mgr, make_issue,
                                                block):
        # These rows share a partition with the blocking Issue's INFO row; the
        # begins_with(SK, "800#BLOCKEDISSUE#") guard is what keeps it out.
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        blocking, _ = mgr.get_issue_blocking(
            space_id=ctv.space_id, blocking_issue_id=blocker.issue_id)

        assert len(blocking) == 1
        assert all(not hasattr(b, "title") for b in blocking)

    def test_excludes_inbound_blockers(self, ctv, mgr, make_issue, block):
        # What X blocks, not what blocks X.
        x = make_issue("x")
        y = make_issue("y")
        block(y, x)

        blocking, _ = mgr.get_issue_blocking(space_id=ctv.space_id,
                                             blocking_issue_id=x.issue_id)

        assert blocking == []

    def test_includes_cross_space_targets(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        remote = make_issue("remote", space_id=ctv.other_space_id)
        block(blocker, remote, blocked_space=ctv.other_space_id)

        blocking, _ = mgr.get_issue_blocking(
            space_id=ctv.space_id, blocking_issue_id=blocker.issue_id)

        assert len(blocking) == 1
        assert blocking[0].blocked_issue_space_id == ctv.other_space_id
        assert blocking[0].blocked_issue_id == remote.issue_id

    def test_includes_satisfied_blockers(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        blocked = make_issue("blocked")
        block(blocker, blocked)

        mgr.transition_issue(space_id=ctv.space_id, issue_id=blocker.issue_id,
                             status=IssueStatus.DONE)
        mgr.handle_issue_done(space_id=ctv.space_id, issue_id=blocker.issue_id)

        blocking, _ = mgr.get_issue_blocking(
            space_id=ctv.space_id, blocking_issue_id=blocker.issue_id)

        assert len(blocking) == 1
        assert blocking[0].is_blocking_issue_done is True

    def test_empty_result(self, ctv, mgr, make_issue):
        issue = make_issue("lonely")

        blocking, cursor = mgr.get_issue_blocking(
            space_id=ctv.space_id, blocking_issue_id=issue.issue_id)

        assert blocking == []
        assert cursor is None

    def test_limit_and_cursor(self, ctv, mgr, make_issue, block):
        blocker = make_issue("blocker")
        for n in range(5):
            block(blocker, make_issue(f"blocked-{n}"))

        page, cursor = mgr.get_issue_blocking(
            space_id=ctv.space_id, blocking_issue_id=blocker.issue_id, limit=2)

        assert len(page) == 2
        assert cursor is not None

    def test_full_pagination_yields_each_row_once(self, ctv, mgr, make_issue,
                                                  block):
        blocker = make_issue("blocker")
        expected = set()
        for n in range(5):
            blocked = make_issue(f"blocked-{n}")
            expected.add(blocked.issue_id)
            block(blocker, blocked)

        blocking, pages = drain(mgr.get_issue_blocking,
                                space_id=ctv.space_id,
                                blocking_issue_id=blocker.issue_id,
                                limit=2)

        assert {b.blocked_issue_id for b in blocking} == expected
        assert pages == 3

    def test_pagination_never_returns_the_info_row(self, ctv, mgr, make_issue,
                                                   block):
        blocker = make_issue("blocker")
        for n in range(4):
            block(blocker, make_issue(f"blocked-{n}"))

        blocking, _ = drain(mgr.get_issue_blocking, space_id=ctv.space_id,
                            blocking_issue_id=blocker.issue_id, limit=1)

        assert len(blocking) == 4
