import base64
import functools
import json
import random
import re
import string
import secrets
import time

from decimal import Decimal
from datetime import datetime, UTC

from .const import (
    MAX_ISSUE_ID_LEN,
    MAX_SPACE_ID_LEN,
    MIN_ISSUE_ID_LEN,
    TRANSACT_RETRY_ATTEMPTS,
    TRANSACT_RETRY_BASE_DELAY,
    TRANSACT_RETRY_MAX_DELAY,
)
from .errors import DDBArgsError, DDBTransactionConflictError


# Defaults to [a-zA-Z0-9]
DEFAULT_ID_ALPHABET = string.ascii_letters + string.digits

# Characters a caller-supplied space_id may use. Excludes "#", the separator
# every key format string is built on.
SPACE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def isotime(dt=None, timespec="milliseconds"):
    if dt is None:
        dt = datetime.now(tz=UTC)

    return dt.isoformat(timespec=timespec).replace("+00:00", "Z")


def cleanup_decimals(d):
    if isinstance(d, Decimal):
        int_d = int(d)
        return int_d if d == int_d else float(d)
    elif isinstance(d, dict):
        for k in d:
            d[k] = cleanup_decimals(d[k])
    elif isinstance(d, list):
        d = [cleanup_decimals(le) for le in d]

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
    except Exception:
        raise DDBArgsError("Invalid exclusive start key")


def decode_pagination_cursor(cursor):
    """
    Decode a pagination cursor to a DynamoDB ExclusiveStartKey.

    Args:
        str: encoded cursor

    Returns:
        dict: DynamoDB exclusive_start_key
    """
    # encode_pagination_cursor strips the padding to keep the cursor tidy, so
    # put back however much this length implies before decoding.
    padded = cursor + "=" * (-len(cursor) % 4)
    json_bytes = base64.urlsafe_b64decode(padded)
    return json.loads(json_bytes.decode())


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
