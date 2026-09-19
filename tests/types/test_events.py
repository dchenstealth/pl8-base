# SPDX-License-Identifier: MIT

import json

import pytest

from pl8_base.types import (
    EVENT_CLASS_MAP,
    IssueDeleted,
    IssueDone,
    IssueNumActiveBlockersZeroed,
    IssueReady,
)


EVENT_CLASSES = [IssueNumActiveBlockersZeroed, IssueDeleted, IssueDone, IssueReady]


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
    event = event_cls(space_id="sp1", issue_id="iss1")

    assert event.dict()["type"] == event_cls.__name__


@pytest.mark.parametrize("event_cls", EVENT_CLASSES)
def test_to_entry_builds_expected_entry(event_cls):
    event = event_cls(space_id="sp1", issue_id="iss1")

    entry = event.to_entry(source="pl8.stream-handler", event_bus_name="pl8-bus")

    assert entry["Source"] == "pl8.stream-handler"
    assert entry["DetailType"] == event_cls.__name__
    assert entry["EventBusName"] == "pl8-bus"
    assert json.loads(entry["Detail"]) == event.dict()


def test_event_class_map_matches_class_names():
    assert set(EVENT_CLASS_MAP.keys()) == {cls.__name__ for cls in EVENT_CLASSES}
    for name, cls in EVENT_CLASS_MAP.items():
        assert cls.__name__ == name
