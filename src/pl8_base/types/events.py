# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

import json
import uuid

import msgspec

from ..util import isotime


class BaseEventMeta(msgspec.StructMeta):
    def __new__(mcls, name, bases, namespace, **struct_config):
        struct_config.setdefault("kw_only", True)
        return super().__new__(mcls, name, bases, namespace, **struct_config)


# tag_field "type" sets the class name as the `type` field when serializing,
# matching BaseObject's convention so consumers decode both the same way.
class BaseEvent(msgspec.Struct, tag=True, tag_field="type",
                metaclass=BaseEventMeta):
    # SemVer string for type versioning, e.g. 0.0.1
    type_version: str

    # Resolved in __post_init__ when not supplied, so None only ever appears
    # between __init__ and that call, never on a constructed event.
    event_id: str | None = None
    sent_at: str | None = None

    def __post_init__(self):
        if not self.event_id:
            self.event_id = str(uuid.uuid4())

        if not self.sent_at:
            self.sent_at = isotime()

    def dict(self):
        return msgspec.to_builtins(self)

    def to_entry(self, *, source, event_bus_name):
        """Build a PutEvents entry for this event.

        Args:
            source (str): EventBridge Source field
            event_bus_name (str): EventBridge bus name to send on

        Returns:
            dict: entry suitable for
                events_client.put_events(Entries=[entry])
        """
        return {
            "Source": source,
            "DetailType": type(self).__name__,
            "Detail": json.dumps(self.dict()),
            "EventBusName": event_bus_name,
        }


class IssueNumActiveBlockersZeroed(BaseEvent):
    """Core lifecycle event.

    Sent when Issue.num_active_blockers becomes 0. Triggers Issue to be
    moved from BLOCKED to TODO.
    """
    type_version: str = "0.0.1"

    space_id: str
    issue_id: str


class IssueDeleted(BaseEvent):
    """Core lifecycle event.

    Sent when Issue is deleted. Triggers cleanup of linked IssueBlockers.
    """
    type_version: str = "0.0.1"

    space_id: str
    issue_id: str


class IssueDone(BaseEvent):
    """Core lifecycle event.

    Sent when an Issue is transitioned to status=DONE. Triggers its
    IssueBlockers to be marked satisfied and the counters on the Issues
    they block to be decremented.
    """
    type_version: str = "0.0.1"

    space_id: str
    issue_id: str


class IssueReady(BaseEvent):
    """Consumer-facing event.

    Sent when a new Issue is created with status=TODO, or when an Issue is
    transitioned to status=TODO.
    """
    type_version: str = "0.0.1"

    space_id: str
    issue_id: str


class IssueCommentDeleted(BaseEvent):
    """Core lifecycle event.

    Sent when an IssueComment is deleted. Triggers the IssueAttachments linked
    to that comment to be deleted, which in turn drives their S3 objects away
    via IssueAttachmentDeleted. No counter moves: the comment that held
    num_attachments over them is the row that is already gone.
    """
    type_version: str = "0.0.1"

    space_id: str
    issue_id: str
    comment_id: str


class IssueAttachmentDeleted(BaseEvent):
    """Core lifecycle event.

    Sent when an IssueAttachment row is deleted, however it was deleted: by a
    caller, by one of the delete sweeps, or by DynamoDB's TTL reaping an upload
    that was never confirmed. Triggers the S3 object to be deleted, so the
    bytes never outlive the row that named them.
    """
    type_version: str = "0.0.1"

    space_id: str
    issue_id: str
    attachment_id: str
