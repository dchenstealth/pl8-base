# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

import json

import boto3
import msgspec
import pytest
from moto import mock_aws

from pl8_base.errors import EventCorruptedError, EventSendError
from pl8_base.types import EVENT_CLASS_MAP, IssueDeleted
from pl8_base.util import parse_event, send_event

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


class FakeEventsClient:
    """Captures put_events calls and returns a canned response."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def put_events(self, Entries):
        self.calls.append(Entries)
        return self.response


def test_send_event_sends_expected_entry():
    event = IssueDeleted(space_id="sp1", issue_id="iss1")
    client = FakeEventsClient({"FailedEntryCount": 0, "Entries": [{"EventId": "abc"}]})

    response = send_event(events_client=client, event=event,
                          source="pl8.stream-handler", event_bus_name="pl8-bus")

    assert client.calls == [[event.to_entry(source="pl8.stream-handler",
                                            event_bus_name="pl8-bus")]]
    assert response["FailedEntryCount"] == 0


def test_send_event_raises_on_failed_entry():
    event = IssueDeleted(space_id="sp1", issue_id="iss1")
    client = FakeEventsClient({
        "FailedEntryCount": 1,
        "Entries": [{"ErrorCode": "InternalFailure", "ErrorMessage": "boom"}],
    })

    with pytest.raises(EventSendError, match="InternalFailure"):
        send_event(events_client=client, event=event,
                   source="pl8.stream-handler", event_bus_name="pl8-bus")


@mock_aws
def test_send_event_against_real_eventbridge_client():
    """Exercises to_entry()'s output against botocore's own validation,
    catching shape bugs a fake client wouldn't (e.g. Detail must be a
    JSON string, not a dict).
    """
    events_client = boto3.client("events", region_name="us-east-1")
    event = IssueDeleted(space_id="sp1", issue_id="iss1")

    response = send_event(events_client=events_client, event=event,
                          source="pl8.stream-handler", event_bus_name="default")

    assert response["FailedEntryCount"] == 0
    assert response["Entries"][0]["EventId"]


@pytest.mark.parametrize("event_cls", EVENT_CLASS_MAP.values())
def test_parse_event_round_trips_each_event_type(event_cls):
    event = make_event(event_cls)

    assert parse_event(event.dict()) == event


def test_parse_event_round_trips_through_sqs_envelope():
    """Mirrors what an SQS-triggered lambda actually receives: the full
    EventBridge envelope as the record body, with our payload nested
    under "detail".
    """
    event = IssueDeleted(space_id="sp1", issue_id="iss1")
    entry = event.to_entry(source="pl8.stream-handler", event_bus_name="pl8-bus")
    sqs_body = json.dumps({
        "version": "0",
        "id": "abc-123",
        "detail-type": entry["DetailType"],
        "source": entry["Source"],
        "detail": json.loads(entry["Detail"]),
    })

    parsed = parse_event(json.loads(sqs_body)["detail"])

    assert parsed == event


def test_parse_event_raises_on_missing_type():
    with pytest.raises(EventCorruptedError, match="without type"):
        parse_event({"space_id": "sp1", "issue_id": "iss1"})


def test_parse_event_raises_on_unknown_type():
    with pytest.raises(EventCorruptedError, match="unknown type"):
        parse_event({"type": "SomeFutureEvent", "space_id": "sp1"})


def test_parse_event_raises_on_malformed_payload():
    with pytest.raises(EventCorruptedError, match="Malformed event"):
        parse_event({"type": "IssueDeleted", "space_id": "sp1"})
