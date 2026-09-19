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

    event_id: str = None
    sent_at: str = None

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
