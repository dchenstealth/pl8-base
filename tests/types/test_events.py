# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

import json

import msgspec
import pytest

from pl8_base.types import (
    EVENT_CLASS_MAP,
    IssueAttachmentDeleted,
    IssueCommentDeleted,
    IssueDeleted,
    IssueDone,
    IssueNumActiveBlockersZeroed,
    IssueReady,
)

EVENT_CLASSES = [IssueNumActiveBlockersZeroed, IssueDeleted, IssueDone,
                 IssueReady, IssueCommentDeleted, IssueAttachmentDeleted]

# Every event's payload is ids, so a test event is built by name rather than
# case by case: a new event type that names another id needs a value here and
# nothing else.
EVENT_ID_VALUES = {
    "space_id": "sp1",
    "issue_id": "iss1",
    "comment_id": "0199f3a1-0000-7000-8000-000000000001",
    "attachment_id": "0199f3a1-0000-7000-8000-0000000000a1",
}


def make_event(event_cls, **overrides):
    """An instance of any BaseEvent subclass, with its id fields filled."""
    kwargs = {field.name: EVENT_ID_VALUES[field.name]
              for field in msgspec.structs.fields(event_cls)
              if field.name in EVENT_ID_VALUES}
    kwargs.update(overrides)
    return event_cls(**kwargs)


def test_generates_event_id_and_sent_at_when_unset():
    event = IssueDeleted(space_id="sp1", issue_id="iss1")

    assert event.event_id
    assert event.sent_at


def test_generated_event_ids_are_unique():
    first = IssueDeleted(space_id="sp1", issue_id="iss1")
    second = IssueDeleted(space_id="sp1", issue_id="iss1")

    assert first.event_id != second.event_id


def test_preserves_explicit_event_id_and_sent_at():
    event = IssueDeleted(space_id="sp1", issue_id="iss1",
                         event_id="fixed-id", sent_at="2026-01-01T00:00:00Z")

    assert event.event_id == "fixed-id"
    assert event.sent_at == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize("event_cls", EVENT_CLASSES)
def test_dict_tags_type_as_class_name(event_cls):
    event = make_event(event_cls)

    assert event.dict()["type"] == event_cls.__name__


@pytest.mark.parametrize("event_cls", EVENT_CLASSES)
def test_to_entry_builds_expected_entry(event_cls):
    event = make_event(event_cls)

    entry = event.to_entry(source="pl8.stream-handler", event_bus_name="pl8-bus")

    assert entry["Source"] == "pl8.stream-handler"
    assert entry["DetailType"] == event_cls.__name__
    assert entry["EventBusName"] == "pl8-bus"
    assert json.loads(entry["Detail"]) == event.dict()


def test_event_class_map_matches_class_names():
    assert set(EVENT_CLASS_MAP.keys()) == {cls.__name__ for cls in EVENT_CLASSES}
    for name, cls in EVENT_CLASS_MAP.items():
        assert cls.__name__ == name


@pytest.mark.parametrize("event_cls, id_field", [
    (IssueCommentDeleted, "comment_id"),
    (IssueAttachmentDeleted, "attachment_id"),
])
def test_names_the_row_it_is_about(event_cls, id_field):
    # A handler is given the ids and nothing else: it recomputes whatever it
    # needs, the S3 key included, rather than trusting a payload to carry it.
    event = make_event(event_cls)

    assert event.dict()[id_field] == EVENT_ID_VALUES[id_field]
    assert set(event.dict()) == {"type", "type_version", "event_id", "sent_at",
                                "space_id", "issue_id", id_field}
