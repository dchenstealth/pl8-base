# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

import base64
import functools
import json
import random
import re
import secrets
import string
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import msgspec

from .const import (
    MAX_ATTACHMENT_NAME_LEN,
    MAX_ATTACHMENT_SIZE_BYTES,
    MAX_CONTENT_TYPE_LEN,
    MAX_CREATOR_LEN,
    MAX_ISSUE_ID_LEN,
    MAX_SPACE_ID_LEN,
    MIN_ISSUE_ID_LEN,
    TRANSACT_RETRY_ATTEMPTS,
    TRANSACT_RETRY_BASE_DELAY,
    TRANSACT_RETRY_MAX_DELAY,
)
from .errors import (
    DDBArgsError,
    DDBTransactionConflictError,
    EventCorruptedError,
    EventSendError,
)

# Defaults to [a-zA-Z0-9]
DEFAULT_ID_ALPHABET = string.ascii_letters + string.digits

# Characters a caller-supplied space_id may use. Excludes "#", the separator
# every key format string is built on.
SPACE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

# Characters an attachment name MUST NOT contain: the C0 controls, DEL, and the
# two characters that would break out of a quoted HTTP header value. See
# validate_attachment_name; everything else, spaces and non-ASCII included, is
# allowed, since a name is a label on the row and never part of a key.
ATTACHMENT_NAME_FORBIDDEN = re.compile(r'[\x00-\x1f\x7f"\\]')

# A media type, as "type/subtype" in RFC 9110 token characters. Parameters are
# deliberately not accepted: the type is signed into the presigned POST policy
# as an exact Content-Type condition, so a caller sending
# "text/plain; charset=utf-8" against a policy signed for "text/plain" would
# have its upload refused by S3 rather than by anything here, which is a far
# worse error to debug. See validate_content_type.
CONTENT_TYPE_PATTERN = re.compile(
    r"[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+")


def isotime(dt=None, timespec="milliseconds"):
    if dt is None:
        dt = datetime.now(tz=UTC)

    return dt.isoformat(timespec=timespec).replace("+00:00", "Z")


def cleanup_decimals(d):
    """
    Replace every Decimal in a structure with an int or a float.

    boto3's TypeDeserializer hands back Decimal for every N attribute, which
    msgspec will not convert to an int or float field. Returns a new
    structure rather than editing in place, so the caller's copy of a
    deserialized item is left as it was.

    Args:
        d: any value, searched recursively through dicts and lists

    Returns:
        the same structure with each Decimal replaced by an int when it is
        integral, otherwise a float
    """
    if isinstance(d, Decimal):
        int_d = int(d)
        return int_d if d == int_d else float(d)

    if isinstance(d, dict):
        return {k: cleanup_decimals(v) for k, v in d.items()}

    if isinstance(d, list):
        return [cleanup_decimals(le) for le in d]

    return d


def gen_issue_id(*, issue_id_len=6, alphabet=None):
    """
    Generate an issue id of the specified length from the specified alphabet.
    Defaults to [a-zA-Z0-9]

    Args:
        issue_id_len (int): id length
        alphabet (sequence): allowed characters

    Returns:
        str: generated issue id

    Raises:
        DDBArgsError: if issue_id_len is out of bounds
    """
    if issue_id_len < MIN_ISSUE_ID_LEN:
        raise DDBArgsError("Issue ID too short")
    elif issue_id_len > MAX_ISSUE_ID_LEN:
        raise DDBArgsError("Issue ID too long")

    if alphabet is None:
        alphabet = DEFAULT_ID_ALPHABET

    return "".join(secrets.choice(alphabet) for _ in range(issue_id_len))


def validate_space_id(space_id):
    """
    Validate a caller-supplied space id.

    Unlike issue ids, space ids come from the caller, so nothing has already
    constrained them. They are interpolated into SPACE#{space_id} and into
    ISSUE#{space_id}#{issue_id}, so a "#" would make both keys ambiguous.

    Args:
        space_id (str): id to validate

    Raises:
        DDBArgsError: if the id is not a string, is empty, is too long, or
            uses characters outside [A-Za-z0-9_-]
    """
    if not isinstance(space_id, str):
        raise DDBArgsError("Space ID must be a string")

    if not space_id:
        raise DDBArgsError("Space ID is empty")

    if len(space_id) > MAX_SPACE_ID_LEN:
        raise DDBArgsError("Space ID too long")

    # fullmatch, not a "$" anchored search: "$" also matches before a trailing
    # newline, so a space_id ending in one would pass and put that newline
    # straight into the key.
    if not SPACE_ID_PATTERN.fullmatch(space_id):
        raise DDBArgsError("Space ID has invalid characters")


def isotime_from_uuid7(value):
    """
    The timestamp a UUIDv7 carries, rendered the way isotime renders one.

    IssueComment uses this to take its created_at from its comment_id rather
    than from a second clock reading, so the two cannot disagree about when the
    comment was written. uuid7 mints from its own clock and takes no timestamp,
    so this is the direction that keeps them in step.

    Args:
        value (str): a UUIDv7

    Returns:
        str: ISO-8601 timestamp, as isotime renders it

    Raises:
        DDBArgsError: if value is not a UUIDv7
    """
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise DDBArgsError(f"Invalid UUID: {value!r}")

    if parsed.version != 7:
        raise DDBArgsError(f"Not a UUIDv7: {value!r}")

    # UUID.time is the 48 bit unix_ts_ms field for a v7
    return isotime(datetime.fromtimestamp(parsed.time / 1000, tz=UTC))


def validate_creator(creator):
    """
    Validate a caller-supplied creator.

    A creator records who or what made a Space, Issue or IssueComment. PL8 has
    no user model, so it is a label the caller supplies, never an identity PL8
    establishes: nothing here checks it against the invoking IAM principal, and
    no operation is allowed or refused on the basis of it. Validation is
    therefore about keeping a sane value in the row, not about trust.

    It never composes a key, unlike a space_id, so nothing constrains its
    characters and "#" is unremarkable in one.

    Args:
        creator (str): creator to validate

    Raises:
        DDBArgsError: if the creator is not a string, is empty, or is too long
    """
    if not isinstance(creator, str):
        raise DDBArgsError("Creator must be a string")

    if not creator:
        raise DDBArgsError("Creator is empty")

    if len(creator) > MAX_CREATOR_LEN:
        raise DDBArgsError("Creator too long")


def validate_issue_status(status):
    """
    Coerce a caller-supplied status to an IssueStatus member.

    msgspec Structs do not type check on direct instantiation, so nothing
    downstream stops an arbitrary string from reaching IssueInfo.status and
    the GSI1PK it composes. Such a row can no longer be read back, since
    from_item does validate, so a bad argument would surface later as
    DDBCorruptedError. Reject it here instead, while it is still an argument.

    Imports IssueStatus from .types locally rather than at module level:
    types/events.py imports isotime from this module, so a top-level import
    here would be circular. Same reason as parse_event below.

    Args:
        status (str or IssueStatus): status to validate

    Returns:
        IssueStatus: the matching member

    Raises:
        DDBArgsError: if status is not one of the four IssueStatus values
    """
    from .types import IssueStatus

    try:
        return IssueStatus(status)
    except ValueError:
        raise DDBArgsError(f"Invalid issue status: {status!r}")


def validate_attachment_status(status):
    """
    Coerce a caller-supplied status to an AttachmentStatus member.

    The same reasoning as validate_issue_status: msgspec Structs do not type
    check on direct instantiation, so nothing downstream stops an arbitrary
    string from reaching IssueAttachment.status. Such a row can no longer be
    read back, since from_item does validate, so a bad argument would surface
    later as DDBCorruptedError. Reject it here instead, while it is still an
    argument.

    Imports AttachmentStatus from .types locally rather than at module level:
    types/events.py imports isotime from this module, so a top-level import
    here would be circular. Same reason as validate_issue_status above.

    Args:
        status (str or AttachmentStatus): status to validate

    Returns:
        AttachmentStatus: the matching member

    Raises:
        DDBArgsError: if status is not one of the AttachmentStatus values
    """
    from .types import AttachmentStatus

    try:
        return AttachmentStatus(status)
    except ValueError:
        raise DDBArgsError(f"Invalid attachment status: {status!r}")


def validate_comment_id(comment_id):
    """
    Validate a comment id.

    A comment id composes a sort key twice over: 500#COMMENT#{comment_id} for
    the comment row itself, and the COMMENTATTACHMENT#... GSI1 partition an
    IssueAttachment is linked under. get_issue_comments_after also builds a sort
    key range out of one. So an id carrying a "#", or a newline, would move the
    key it composes rather than fail to match it.

    Defers the whole check to isotime_from_uuid7, which already raises
    DDBArgsError for anything that is not a UUIDv7, rather than adding a second
    definition of what a comment id is: PL8 mints comment ids as UUIDv7s and
    reads their creation timestamps back out of them, so "is a UUIDv7" is the
    rule, and the characters a UUID can be spelled with are a consequence of it.

    Args:
        comment_id (str): id to validate

    Raises:
        DDBArgsError: if comment_id is not a UUIDv7
    """
    isotime_from_uuid7(comment_id)


def validate_attachment_name(name):
    """
    Validate a caller-supplied attachment name.

    The name is the human-readable filename, and it is deliberately not part of
    the S3 key; see types/issue.py IssueAttachment.S3_KEY_FORMAT. It reaches the
    downloader through the ResponseContentDisposition of a presigned GET, as
    `attachment; filename="<name>"`, and that header value is *signed into* the
    URL. A `"` would close the quoted string early and a control character, a
    newline in particular, would end the header line, so either one is an
    injection into a signed header the caller does not otherwise control. A
    backslash is refused for the same reason: in a quoted string it escapes the
    next character, so a name ending in one escapes the closing quote.

    Otherwise unconstrained: spaces, punctuation and non-ASCII are all fine in
    a filename, and unlike a space_id the name never composes a key.

    Args:
        name (str): name to validate

    Raises:
        DDBArgsError: if the name is not a string, is empty, is too long, or
            contains a control character, a quote or a backslash
    """
    if not isinstance(name, str):
        raise DDBArgsError("Attachment name must be a string")

    if not name:
        raise DDBArgsError("Attachment name is empty")

    if len(name) > MAX_ATTACHMENT_NAME_LEN:
        raise DDBArgsError("Attachment name too long")

    if ATTACHMENT_NAME_FORBIDDEN.search(name):
        raise DDBArgsError("Attachment name has invalid characters")


def validate_content_type(content_type):
    """
    Validate a caller-supplied attachment content type.

    The content type is signed into the presigned POST twice, as a policy
    condition and as a field, so S3 refuses an upload that declares anything
    else. That makes it worth checking here: a value that is not a media type
    at all would still be signed, and the upload would fail at S3 with the
    caller holding a URL it can never use.

    Checked as a shape rather than against a list of known types. PL8 attaches
    whatever a caller attaches and has no opinion on the format, so an
    allowlist would only mean rejecting next year's media types.

    Args:
        content_type (str): content type to validate

    Raises:
        DDBArgsError: if the content type is not a string, is empty, is too
            long, or is not "type/subtype" in token characters
    """
    if not isinstance(content_type, str):
        raise DDBArgsError("Content type must be a string")

    if not content_type:
        raise DDBArgsError("Content type is empty")

    if len(content_type) > MAX_CONTENT_TYPE_LEN:
        raise DDBArgsError("Content type too long")

    # fullmatch rather than a "$" anchored search, for the same reason
    # validate_space_id uses one: "$" also matches before a trailing newline.
    if not CONTENT_TYPE_PATTERN.fullmatch(content_type):
        raise DDBArgsError("Invalid content type")


def validate_attachment_size(size):
    """
    Validate a caller-supplied attachment size in bytes.

    The size is declared before the bytes exist, because it becomes an exact
    content-length-range condition in the presigned POST policy: S3 then
    refuses a body of any other length, which is what stops a caller declaring
    one byte and uploading a gigabyte. So this bound is enforced at signing
    time, on a number, rather than by measuring anything.

    Zero is refused along with the negatives. An empty object is representable
    in S3, but an attachment of nothing is a caller bug far more often than an
    intent, and a zero-length range condition is an awkward thing to have
    signed.

    A bool is refused even though it is an int in Python: True would otherwise
    be a legal size of one byte.

    Args:
        size (int): size in bytes

    Raises:
        DDBArgsError: if size is not an int, or is outside
            1..MAX_ATTACHMENT_SIZE_BYTES
    """
    if isinstance(size, bool) or not isinstance(size, int):
        raise DDBArgsError("Attachment size must be an integer")

    if size < 1:
        raise DDBArgsError("Attachment size must be at least 1 byte")

    if size > MAX_ATTACHMENT_SIZE_BYTES:
        raise DDBArgsError("Attachment size too large")


def encode_pagination_cursor(exclusive_start_key):
    """
    Encode a pagination cursor from a DynamoDB ExclusiveStartKey.

    Args:
        dict: DynamoDB exclusive_start_key

    Returns:
        str: url safe str

    Raises:
        DDBArgsError: on invalid input
    """
    try:
        json_bytes = json.dumps(exclusive_start_key).encode()
        return base64.urlsafe_b64encode(json_bytes).decode().rstrip("=")
    except (TypeError, ValueError):
        raise DDBArgsError("Invalid exclusive start key")


def decode_pagination_cursor(cursor):
    """
    Decode a pagination cursor to a DynamoDB ExclusiveStartKey.

    A cursor is round-tripped through whatever client is paging, so it arrives
    here as untrusted input: everything it can fail on is reported as one
    DDBArgsError rather than leaking a base64 or JSON error to the caller.

    Args:
        str: encoded cursor

    Returns:
        dict: DynamoDB exclusive_start_key

    Raises:
        DDBArgsError: if the cursor is not a well-formed encoded start key
    """
    try:
        # encode_pagination_cursor strips the padding to keep the cursor tidy,
        # so put back however much this length implies before decoding.
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (TypeError, ValueError):
        # binascii.Error, JSONDecodeError and UnicodeDecodeError all subclass
        # ValueError; a non-string cursor fails the concatenation with a
        # TypeError.
        raise DDBArgsError("Invalid pagination cursor")

    # An ExclusiveStartKey is a flat map of attr name to AttributeValue.
    # Checking the shape here keeps a malformed cursor a bad argument rather
    # than something boto3 rejects as a malformed request further down.
    if not isinstance(decoded, dict) or not all(
            isinstance(k, str) and isinstance(v, dict)
            for k, v in decoded.items()):
        raise DDBArgsError("Invalid pagination cursor")

    return decoded


def retry_on_transaction_conflict(*, attempts=TRANSACT_RETRY_ATTEMPTS,
                                  base_delay=TRANSACT_RETRY_BASE_DELAY,
                                  max_delay=TRANSACT_RETRY_MAX_DELAY):
    """
    Retry the decorated call on DDBTransactionConflictError.

    Uses full-jitter exponential backoff: attempt n sleeps a uniform random
    interval in [0, min(max_delay, base_delay * 2**n)). Drawing across the
    whole interval rather than jittering around a fixed backoff is what keeps
    conflicting writers from re-colliding in lockstep.

    Only DDBTransactionConflictError is retried. DDBVersionConflictError is
    deliberately not: it means the caller's view of the item is stale, so the
    same request would keep failing until the caller re-reads.

    Args:
        attempts (int): total tries, including the first
        base_delay (float): seconds, the exponential's starting point
        max_delay (float): seconds, cap on the interval drawn from

    Returns:
        callable: decorator

    Raises:
        DDBArgsError: if attempts < 1
        DDBTransactionConflictError: if every attempt conflicts
    """
    if attempts < 1:
        raise DDBArgsError("attempts must be at least 1")

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except DDBTransactionConflictError:
                    if attempt == attempts - 1:
                        raise

                    # Jitter is not security sensitive, so random not secrets
                    interval = min(max_delay, base_delay * (2 ** attempt))
                    time.sleep(random.uniform(0, interval))

        return wrapper

    return decorator


def send_event(*, events_client, event, source, event_bus_name):
    """Send a single BaseEvent on an EventBridge bus.

    Takes the EventBridge client explicitly rather than depending on a
    manager instance carrying one, so callers that never send events (e.g.
    most PL8DDB consumers) never need to configure one.

    Args:
        events_client (boto3 EventBridge client): client to send with
        event (BaseEvent): event to send
        source (str): EventBridge Source field
        event_bus_name (str): EventBridge bus name to send on

    Returns:
        dict: put_events response

    Raises:
        EventSendError: if EventBridge reports a failed entry
    """
    entry = event.to_entry(source=source, event_bus_name=event_bus_name)
    response = events_client.put_events(Entries=[entry])

    if response.get("FailedEntryCount"):
        failed = response["Entries"][0]
        raise EventSendError(
            f"Failed to send event {type(event).__name__} "
            f"(code: {failed.get('ErrorCode')}): {failed.get('ErrorMessage')}"
        )

    return response


def parse_event(detail):
    """Parse an EventBridge event Detail dict into a typed BaseEvent.

    Consumers reading off the SQS queue behind the eventbus get the full
    EventBridge envelope as the SQS record body; this parses the inner
    "detail" dict, e.g.:
        parse_event(json.loads(record["body"])["detail"])

    Imports EVENT_CLASS_MAP from .types locally rather than at module level:
    types/events.py imports isotime from this module, so a top-level import
    here would be circular.

    Args:
        detail (dict): the event's "detail" field

    Returns:
        BaseEvent: parsed event, of the concrete subclass named by detail["type"]

    Raises:
        EventCorruptedError: if missing/unknown type, or malformed payload
    """
    from .types import EVENT_CLASS_MAP

    event_type = detail.get("type")
    if event_type is None:
        raise EventCorruptedError("Malformed event without type")

    event_cls = EVENT_CLASS_MAP.get(event_type)
    if event_cls is None:
        raise EventCorruptedError(f"Event with unknown type: {event_type}")

    try:
        return msgspec.convert(detail, event_cls)
    except (TypeError, ValueError) as exc:
        # msgspec.ValidationError subclasses ValueError
        raise EventCorruptedError(f"Malformed event: {exc!s}")
