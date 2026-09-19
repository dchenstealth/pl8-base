"""Space CRUD and enumeration.

A Space stores Space metadata and makes every space id enumerable. It holds no
referential integrity over Issues in either direction: an Issue may name a Space
that was never created, and deleting a Space leaves its Issues in place. That
contract is enforced at the caller level, and TestSpaceIssueIndependence is what
keeps it from being tightened here by accident.
"""

import gzip

import pytest

from pl8_base.const import MAX_SPACE_ID_LEN
from pl8_base.errors import (
    DDBArgsError,
    DDBExistsError,
    DDBMissingError,
    DDBVersionConflictError,
)
from pl8_base.types import IssueStatus


@pytest.fixture
def new_space(ctv, mgr):
    """A freshly created Space."""
    return mgr.create_space(space_id=ctv.space_id,
                            name="Engineering",
                            description="test desc")


def space_keys(space):
    return (space.PK, space.SK, space.GSI1PK, space.GSI1SK)


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


class TestCreateSpace:
    def test_returns_space_info(self, ctv, new_space):
        assert new_space.space_id == ctv.space_id
        assert new_space.name == "Engineering"
        assert new_space.description == "test desc"
        assert new_space.version == 1

    def test_returned_object_matches_what_was_stored(self, ctv, mgr, new_space):
        assert mgr.get_space(space_id=ctv.space_id) == new_space

    def test_writes_exactly_one_row(self, new_space, scan_all):
        assert len(scan_all()) == 1

    def test_row_keys_are_exact(self, ctv, new_space, get_raw):
        row = get_raw(f"SPACE#{ctv.space_id}", "100#INFO")

        assert row is not None
        assert row["PK"] == {"S": f"SPACE#{ctv.space_id}"}
        assert row["SK"] == {"S": "100#INFO"}
        assert row["GSI1PK"] == {"S": "SPACES"}
        assert row["GSI1SK"] == {"S": f"SPACE#{ctv.space_id}"}

    def test_row_is_tagged_with_its_type(self, new_space, get_raw):
        assert get_raw(new_space.PK, new_space.SK)["type"] == {"S": "SpaceInfo"}

    def test_description_is_stored_compressed(self, new_space, get_raw):
        stored = get_raw(new_space.PK, new_space.SK)["description"]

        assert "S" not in stored
        assert gzip.decompress(stored["B"]).decode() == "test desc"

    def test_timestamps_are_set(self, new_space):
        assert new_space.created_at
        assert new_space.updated_at == new_space.created_at

    def test_uses_the_caller_supplied_id(self, mgr):
        # Unlike an Issue, the id is not generated, so it comes back verbatim.
        space = mgr.create_space(space_id="my-space_1", name="n",
                                 description="d")
        assert space.space_id == "my-space_1"
        assert space.PK == "SPACE#my-space_1"

    def test_every_space_lands_in_the_same_gsi_bucket(self, ctv, mgr, get_raw):
        first = mgr.create_space(space_id=ctv.space_id, name="a",
                                 description="d")
        second = mgr.create_space(space_id=ctv.other_space_id, name="b",
                                  description="d")

        assert get_raw(first.PK, first.SK)["GSI1PK"] == {"S": "SPACES"}
        assert get_raw(second.PK, second.SK)["GSI1PK"] == {"S": "SPACES"}
        assert first.GSI1SK != second.GSI1SK

    def test_duplicate_id_raises(self, ctv, mgr, new_space):
        with pytest.raises(DDBExistsError):
            mgr.create_space(space_id=ctv.space_id, name="second",
                             description="second desc")

    def test_duplicate_id_leaves_the_existing_space_untouched(self, ctv, mgr,
                                                              new_space,
                                                              scan_all):
        # The id is the caller's, so a clash is a real conflict rather than
        # something to reroll past the way create_issue does.
        with pytest.raises(DDBExistsError):
            mgr.create_space(space_id=ctv.space_id, name="second",
                             description="second desc")

        assert len(scan_all()) == 1
        assert mgr.get_space(space_id=ctv.space_id) == new_space

    def test_rejects_an_invalid_id(self, mgr):
        with pytest.raises(DDBArgsError):
            mgr.create_space(space_id="ENG#OPS", name="n", description="d")

    def test_an_invalid_id_writes_nothing(self, mgr, scan_all):
        with pytest.raises(DDBArgsError):
            mgr.create_space(space_id="ENG#OPS", name="n", description="d")

        assert scan_all() == []

    def test_rejects_an_overlong_id(self, mgr):
        with pytest.raises(DDBArgsError):
            mgr.create_space(space_id="x" * (MAX_SPACE_ID_LEN + 1), name="n",
                             description="d")

    def test_keyword_only(self, ctv, mgr):
        with pytest.raises(TypeError):
            mgr.create_space(ctv.space_id, "Engineering", "test desc")


class TestGetSpace:
    def test_returns_the_space(self, ctv, mgr, new_space):
        assert mgr.get_space(space_id=ctv.space_id) == new_space

    def test_round_trips_the_compressed_description(self, ctv, mgr, new_space):
        assert mgr.get_space(space_id=ctv.space_id).description == "test desc"

    def test_missing_space_raises(self, ctv, mgr):
        with pytest.raises(DDBMissingError):
            mgr.get_space(space_id="NOSUCH")

    def test_invalid_id_raises_args_not_missing(self, mgr):
        # A malformed id is the caller's mistake, not an absent row.
        with pytest.raises(DDBArgsError):
            mgr.get_space(space_id="ENG#OPS")

    def test_keyword_only(self, ctv, mgr, new_space):
        with pytest.raises(TypeError):
            mgr.get_space(ctv.space_id)


class TestGetSpaces:
    def test_returns_every_space(self, ctv, mgr):
        mgr.create_space(space_id=ctv.space_id, name="a", description="d")
        mgr.create_space(space_id=ctv.other_space_id, name="b", description="d")

        spaces, _ = mgr.get_spaces()

        assert {s.space_id for s in spaces} == {ctv.space_id,
                                                ctv.other_space_id}

    def test_empty_result(self, mgr):
        spaces, cursor = mgr.get_spaces()

        assert spaces == []
        assert cursor is None

    def test_sorts_by_space_id(self, mgr):
        # Creation order must not leak into the listing.
        for space_id in ("ZED", "ALPHA", "MID"):
            mgr.create_space(space_id=space_id, name=space_id, description="d")

        spaces, _ = mgr.get_spaces()

        assert [s.space_id for s in spaces] == ["ALPHA", "MID", "ZED"]

    def test_returns_fully_populated_spaces(self, ctv, mgr):
        mgr.create_space(space_id=ctv.space_id, name="Engineering",
                         description="test desc")

        spaces, _ = mgr.get_spaces()

        # GSI1 projects ALL, so the description comes back decompressed too.
        assert spaces[0].name == "Engineering"
        assert spaces[0].description == "test desc"

    def test_excludes_issue_rows(self, ctv, mgr):
        mgr.create_space(space_id=ctv.space_id, name="a", description="d")
        mgr.create_issue(space_id=ctv.space_id, title="t", description="d",
                         status=IssueStatus.TODO)

        spaces, _ = mgr.get_spaces()

        assert [s.space_id for s in spaces] == [ctv.space_id]

    def test_limit_is_honored(self, mgr):
        for n in range(5):
            mgr.create_space(space_id=f"SPACE-{n}", name="n", description="d")

        spaces, cursor = mgr.get_spaces(limit=2)

        assert len(spaces) == 2
        assert cursor is not None
        assert isinstance(cursor, str)

    def test_no_cursor_on_the_last_page(self, ctv, mgr):
        mgr.create_space(space_id=ctv.space_id, name="only", description="d")

        spaces, cursor = mgr.get_spaces(limit=2)

        assert len(spaces) == 1
        assert cursor is None

    def test_cursor_continues_without_overlap(self, mgr):
        created = [mgr.create_space(space_id=f"SPACE-{n}", name="n",
                                    description="d")
                   for n in range(5)]

        first, cursor = mgr.get_spaces(limit=2)
        second, _ = mgr.get_spaces(limit=2, cursor=cursor)

        assert [s.space_id for s in first] == [s.space_id
                                               for s in created[:2]]
        assert [s.space_id for s in second] == [s.space_id
                                                for s in created[2:4]]

    def test_full_pagination_yields_each_space_once(self, mgr):
        created = [mgr.create_space(space_id=f"SPACE-{n}", name="n",
                                    description="d")
                   for n in range(5)]

        spaces, pages = drain(mgr.get_spaces, limit=2)

        assert [s.space_id for s in spaces] == [s.space_id for s in created]
        assert pages == 3

    def test_ordering_survives_pagination(self, mgr):
        for space_id in ("ZED", "ALPHA", "MID"):
            mgr.create_space(space_id=space_id, name=space_id, description="d")

        spaces, _ = drain(mgr.get_spaces, limit=1)

        assert [s.space_id for s in spaces] == ["ALPHA", "MID", "ZED"]

    def test_keyword_only(self, mgr, new_space):
        with pytest.raises(TypeError):
            mgr.get_spaces(10)


class TestUpdateSpace:
    def test_changes_name_and_description(self, ctv, mgr, new_space):
        mgr.update_space(space_id=ctv.space_id, name="Ops",
                         description="new desc")

        loaded = mgr.get_space(space_id=ctv.space_id)
        assert loaded.name == "Ops"
        assert loaded.description == "new desc"

    def test_name_survives_being_a_reserved_word(self, ctv, mgr, new_space,
                                                 get_raw):
        # NAME is a DynamoDB reserved word. _build_update aliases every attr
        # through ExpressionAttributeNames, and this is the first attribute in
        # the repo actually called "name".
        mgr.update_space(space_id=ctv.space_id, name="renamed",
                         description="d")

        assert get_raw(new_space.PK, new_space.SK)["name"] == {"S": "renamed"}

    def test_bumps_version(self, ctv, mgr, new_space):
        mgr.update_space(space_id=ctv.space_id, name="Ops", description="d")

        assert mgr.get_space(space_id=ctv.space_id).version == 2

    def test_sets_updated_at_and_leaves_created_at(self, ctv, mgr, new_space):
        mgr.update_space(space_id=ctv.space_id, name="Ops", description="d")

        loaded = mgr.get_space(space_id=ctv.space_id)
        assert loaded.created_at == new_space.created_at
        assert loaded.updated_at >= new_space.updated_at

    def test_leaves_keys_alone(self, ctv, mgr, new_space):
        mgr.update_space(space_id=ctv.space_id, name="Ops", description="d")

        assert space_keys(mgr.get_space(space_id=ctv.space_id)) == space_keys(
            new_space)

    def test_keeps_the_description_compressed(self, ctv, mgr, new_space,
                                              get_raw):
        mgr.update_space(space_id=ctv.space_id, name="Ops",
                         description="new desc")

        stored = get_raw(new_space.PK, new_space.SK)["description"]
        assert "S" not in stored
        assert gzip.decompress(stored["B"]).decode() == "new desc"

    def test_matching_version_applies(self, ctv, mgr, new_space):
        mgr.update_space(space_id=ctv.space_id, name="Ops", description="d",
                         version=new_space.version)

        assert mgr.get_space(space_id=ctv.space_id).name == "Ops"

    def test_stale_version_is_rejected(self, ctv, mgr, new_space):
        mgr.update_space(space_id=ctv.space_id, name="first", description="d")

        with pytest.raises(DDBVersionConflictError):
            mgr.update_space(space_id=ctv.space_id, name="second",
                             description="d", version=new_space.version)

    def test_stale_version_leaves_the_row_untouched(self, ctv, mgr, new_space):
        mgr.update_space(space_id=ctv.space_id, name="first", description="d")

        with pytest.raises(DDBVersionConflictError):
            mgr.update_space(space_id=ctv.space_id, name="second",
                             description="d", version=new_space.version)

        loaded = mgr.get_space(space_id=ctv.space_id)
        assert loaded.name == "first"
        assert loaded.version == 2

    def test_missing_space_raises(self, mgr):
        with pytest.raises(DDBMissingError):
            mgr.update_space(space_id="NOSUCH", name="n", description="d")

    def test_missing_space_raises_missing_not_version_conflict(self, mgr):
        # A caller passing a version against a deleted Space needs to hear that
        # it is gone, not that their read was stale.
        with pytest.raises(DDBMissingError):
            mgr.update_space(space_id="NOSUCH", name="n", description="d",
                             version=1)

    def test_invalid_id_raises_args_not_missing(self, mgr):
        with pytest.raises(DDBArgsError):
            mgr.update_space(space_id="ENG#OPS", name="n", description="d")

    def test_keyword_only(self, ctv, mgr, new_space):
        with pytest.raises(TypeError):
            mgr.update_space(ctv.space_id, "Ops", "d")


class TestDeleteSpace:
    def test_removes_the_space(self, ctv, mgr, new_space):
        mgr.delete_space(space_id=ctv.space_id)

        with pytest.raises(DDBMissingError):
            mgr.get_space(space_id=ctv.space_id)

    def test_removes_the_row(self, ctv, mgr, new_space, get_raw):
        mgr.delete_space(space_id=ctv.space_id)
        assert get_raw(new_space.PK, new_space.SK) is None

    def test_drops_it_from_the_listing(self, ctv, mgr, new_space):
        mgr.create_space(space_id=ctv.other_space_id, name="b", description="d")

        mgr.delete_space(space_id=ctv.space_id)

        spaces, _ = mgr.get_spaces()
        assert [s.space_id for s in spaces] == [ctv.other_space_id]

    def test_leaves_other_spaces_alone(self, ctv, mgr, new_space, scan_all):
        other = mgr.create_space(space_id=ctv.other_space_id, name="b",
                                 description="d")

        mgr.delete_space(space_id=ctv.space_id)

        assert len(scan_all()) == 1
        assert mgr.get_space(space_id=ctv.other_space_id) == other

    def test_missing_space_raises(self, mgr):
        with pytest.raises(DDBMissingError):
            mgr.delete_space(space_id="NOSUCH")

    def test_deleting_twice_raises_the_second_time(self, ctv, mgr, new_space):
        mgr.delete_space(space_id=ctv.space_id)

        with pytest.raises(DDBMissingError):
            mgr.delete_space(space_id=ctv.space_id)

    def test_invalid_id_raises_args_not_missing(self, mgr):
        with pytest.raises(DDBArgsError):
            mgr.delete_space(space_id="ENG#OPS")

    def test_keyword_only(self, ctv, mgr, new_space):
        with pytest.raises(TypeError):
            mgr.delete_space(ctv.space_id)


class TestSpaceIssueIndependence:
    """Spaces and Issues do not constrain each other.

    Referential integrity is the caller's job. These are the tests that keep
    this library from quietly acquiring it.
    """

    def test_an_issue_needs_no_space(self, mgr, scan_all):
        issue = mgr.create_issue(space_id="NOSUCH", title="t", description="d",
                                 status=IssueStatus.TODO)

        assert mgr.get_issue(space_id="NOSUCH",
                             issue_id=issue.issue_id) == issue
        # No Space row was conjured into being alongside it.
        assert len(scan_all()) == 1

    def test_deleting_a_space_leaves_its_issues(self, ctv, mgr, new_space,
                                                scan_all):
        first = mgr.create_issue(space_id=ctv.space_id, title="a",
                                 description="d", status=IssueStatus.TODO)
        second = mgr.create_issue(space_id=ctv.space_id, title="b",
                                  description="d", status=IssueStatus.TODO)

        mgr.delete_space(space_id=ctv.space_id)

        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=first.issue_id) == first
        assert mgr.get_issue(space_id=ctv.space_id,
                             issue_id=second.issue_id) == second
        assert len(scan_all()) == 2

    def test_orphaned_issues_are_still_queryable(self, ctv, mgr, new_space):
        mgr.create_issue(space_id=ctv.space_id, title="a", description="d",
                         status=IssueStatus.TODO)

        mgr.delete_space(space_id=ctv.space_id)

        issues, _ = mgr.get_issues_by_status(space_id=ctv.space_id,
                                             status=IssueStatus.TODO)
        assert [i.title for i in issues] == ["a"]

    def test_a_space_and_an_issue_occupy_separate_partitions(self, ctv, mgr,
                                                             new_space,
                                                             scan_all):
        issue = mgr.create_issue(space_id=ctv.space_id, title="a",
                                 description="d", status=IssueStatus.TODO)

        assert new_space.PK != issue.PK
        assert len(scan_all()) == 2

    def test_deleting_an_issue_leaves_its_space(self, ctv, mgr, new_space):
        issue = mgr.create_issue(space_id=ctv.space_id, title="a",
                                 description="d", status=IssueStatus.TODO)

        mgr.delete_issue(space_id=ctv.space_id, issue_id=issue.issue_id)

        assert mgr.get_space(space_id=ctv.space_id) == new_space
