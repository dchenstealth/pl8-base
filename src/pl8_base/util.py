import base64
import json
import string
import secrets

from decimal import Decimal
from datetime import datetime, UTC

from .const import MAX_ISSUE_ID_LEN, MIN_ISSUE_ID_LEN
from .errors import DDBArgsError


# Defaults to [a-zA-Z0-9]
DEFAULT_ID_ALPHABET = string.ascii_letters + string.digits


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
        dict: url safe str
    """
    json_bytes = base64.urlsafe_b64decode(cursor)
    return json.loads(json_bytes.decode())
