# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""Creator, across every entity that records one.

A creator is a label the caller supplies, never an identity pl8-base
establishes: nothing checks it against the invoking IAM principal, and no
operation is allowed or refused on the basis of it. These tests pin that it is
recorded faithfully, that it is fixed at creation, and that it is not treated
as trust; see pl8-docs architecture/entities.md.

Cross-entity on purpose. The per-entity modules cover their own CRUD; this one
covers the rule the three share, so a fourth entity that grows a creator has an
obvious place to be added.
"""

from itertools import count

import pytest

from pl8_base.const import MAX_CREATOR_LEN
from pl8_base.errors import DDBArgsError
from pl8_base.types import IssueStatus

pytestmark = pytest.mark.usefixtures("spaces")


@pytest.fixture
def entities(ctv, mgr):
    """Create/read/update for each entity that records a creator.

    Each entry creates with the given creator and hands back a read and an
    update that take no creator, since nothing may change one after creation.
    """
    made_count = count()

    def space(creator):
        # A fresh id per call, derived from a counter rather than from the
        # creator: a creator may hold characters a space_id may not, or not be
        # a string at all, and those are the cases under test.
        space_id = f"{ctv.space_id}-{next(made_count)}"
        made = mgr.create_space(space_id=space_id, name="n", description="d",
                                creator=creator)
        return SimpleEntity(
            made=made,
            read=lambda: mgr.get_space(space_id=space_id),
            update=lambda: mgr.update_space(space_id=space_id, name="new",
                                            description="new"),
        )

    def issue(creator):
        made = mgr.create_issue(space_id=ctv.space_id, title="t",
                                description="d", status=IssueStatus.TODO,
                                creator=creator)
        return SimpleEntity(
            made=made,
            read=lambda: mgr.get_issue(space_id=ctv.space_id,
                                       issue_id=made.issue_id),
            update=lambda: mgr.update_issue(space_id=ctv.space_id,
                                            issue_id=made.issue_id,
                                            title="new", description="new"),
        )

    def comment(creator):
        issue_info = mgr.create_issue(space_id=ctv.space_id, title="t",
                                      description="d",
                                      status=IssueStatus.TODO,
                                      creator="tester")
        made = mgr.create_issue_comment(space_id=ctv.space_id,
                                        issue_id=issue_info.issue_id,
                                        body="b", creator=creator)
        return SimpleEntity(
            made=made,
            read=lambda: mgr.get_issue_comment(
                space_id=ctv.space_id, issue_id=issue_info.issue_id,
                comment_id=made.comment_id),
            update=lambda: mgr.update_issue_comment(
                space_id=ctv.space_id, issue_id=issue_info.issue_id,
                comment_id=made.comment_id, body="new"),
        )

    return {"Space": space, "Issue": issue, "IssueComment": comment}


class SimpleEntity:
    def __init__(self, *, made, read, update):
        self.made = made
        self.read = read
        self.update = update


ENTITIES = ["Space", "Issue", "IssueComment"]


@pytest.mark.parametrize("entity", ENTITIES)
class TestCreatorIsRecorded:
    def test_create_returns_it(self, entities, entity):
        assert entities[entity]("alice").made.creator == "alice"

    def test_it_round_trips(self, entities, entity):
        made = entities[entity]("alice")

        assert made.read().creator == "alice"

    def test_it_survives_an_update(self, entities, entity):
        # Fixed at creation: an update replaces the editable fields only.
        made = entities[entity]("alice")

        assert made.update().creator == "alice"
        assert made.read().creator == "alice"


@pytest.mark.parametrize("entity", ENTITIES)
class TestCreatorIsNotVerified:
    def test_any_label_is_accepted(self, entities, entity):
        # pl8-base has no user model, so there is nothing to check a creator
        # against. It is provenance for readers, not authorization.
        claimed = "root@example.com"

        assert entities[entity](claimed).made.creator == claimed

    def test_two_entities_may_claim_the_same_creator(self, entities, entity):
        first = entities[entity]("alice")
        second = entities[entity]("alice")

        assert first.made.creator == second.made.creator == "alice"

    def test_characters_a_space_id_may_not_use_are_fine(self, entities,
                                                        entity):
        # A creator never composes a key, so "#" is unremarkable in one.
        claimed = "agent:claude #1"

        assert entities[entity](claimed).made.creator == claimed

    def test_the_maximum_length_is_accepted(self, entities, entity):
        claimed = "x" * MAX_CREATOR_LEN

        assert entities[entity](claimed).made.creator == claimed


@pytest.mark.parametrize("entity", ENTITIES)
class TestCreatorIsValidated:
    def test_empty_is_rejected(self, entities, entity):
        with pytest.raises(DDBArgsError, match="empty"):
            entities[entity]("")

    def test_too_long_is_rejected(self, entities, entity):
        with pytest.raises(DDBArgsError, match="too long"):
            entities[entity]("x" * (MAX_CREATOR_LEN + 1))

    def test_a_non_string_is_rejected(self, entities, entity):
        with pytest.raises(DDBArgsError, match="must be a string"):
            entities[entity](1)


@pytest.mark.parametrize("entity", ENTITIES)
def test_creator_is_required(entities, entity):
    # Required at creation rather than defaulted, so nothing is recorded as
    # having been created by nobody.
    with pytest.raises(TypeError):
        entities[entity]()
