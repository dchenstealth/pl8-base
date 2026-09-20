# SPDX-License-Identifier: MIT

import base64
import json

from datetime import datetime, timedelta, timezone, UTC
from decimal import Decimal

import pytest

from pl8_base.const import (
    MAX_ISSUE_ID_LEN,
    MAX_SPACE_ID_LEN,
    MIN_ISSUE_ID_LEN,
)
from pl8_base.errors import (
    DDBArgsError,
    DDBTransactionConflictError,
    DDBVersionConflictError,
)
from pl8_base.types import IssueStatus
from pl8_base.util import (
    DEFAULT_ID_ALPHABET,
    cleanup_decimals,
    decode_pagination_cursor,
    encode_pagination_cursor,
    gen_issue_id,
    isotime,
    retry_on_transaction_conflict,
    validate_issue_status,
    validate_space_id,
)


class TestIsotime:
    def test_default_is_utc_now_with_z_suffix(self):
        before = datetime.now(tz=UTC)
        result = isotime()
        after = datetime.now(tz=UTC)

        assert result.endswith("Z")
        assert "+00:00" not in result

        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
        # Truncation to milliseconds can put the parsed value a hair before
        # `before`, so allow that much slack on the lower bound.
        assert before - timedelta(milliseconds=1) <= parsed <= after

    def test_millisecond_precision_by_default(self):
        dt = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
        assert isotime(dt) == "2026-01-02T03:04:05.123Z"

    def test_explicit_timespec(self):
        dt = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
        assert isotime(dt, timespec="seconds") == "2026-01-02T03:04:05Z"
        assert isotime(dt, timespec="microseconds") == "2026-01-02T03:04:05.123456Z"

    def test_non_utc_offset_is_left_intact(self):
        # Only the +00:00 offset is rewritten to Z; other offsets are not UTC
        # and must not be mislabelled as such.
        dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=-5)))
        result = isotime(dt)
        assert not result.endswith("Z")
        assert result.endswith("-05:00")

    def test_output_sorts_lexicographically_by_time(self):
        # GSI1SK embeds this value and relies on string ordering matching
        # chronological ordering.
        earlier = isotime(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
        later = isotime(datetime(2026, 1, 2, 3, 4, 6, tzinfo=UTC))
        assert earlier < later


class TestCleanupDecimals:
    def test_integral_decimal_becomes_int(self):
        result = cleanup_decimals(Decimal("5"))
        assert result == 5
        assert isinstance(result, int)

    def test_fractional_decimal_becomes_float(self):
        result = cleanup_decimals(Decimal("1.5"))
        assert result == 1.5
        assert isinstance(result, float)

    def test_negative_and_zero(self):
        assert cleanup_decimals(Decimal("0")) == 0
        assert isinstance(cleanup_decimals(Decimal("0")), int)
        assert cleanup_decimals(Decimal("-3")) == -3
        assert cleanup_decimals(Decimal("-2.5")) == -2.5

    def test_recurses_into_dicts(self):
        result = cleanup_decimals({"a": Decimal("1"), "b": {"c": Decimal("2.5")}})
        assert result == {"a": 1, "b": {"c": 2.5}}
        assert isinstance(result["a"], int)
        assert isinstance(result["b"]["c"], float)

    def test_recurses_into_lists(self):
        result = cleanup_decimals([Decimal("1"), [Decimal("2")], {"k": Decimal("3")}])
        assert result == [1, [2], {"k": 3}]

    def test_passes_through_other_types(self):
        value = {"s": "text", "b": True, "n": None, "f": 1.25, "i": 7}
        assert cleanup_decimals(value) == value

    def test_bools_are_not_coerced(self):
        result = cleanup_decimals({"flag": False})
        assert result["flag"] is False



class TestGenIssueId:
    def test_default_length(self):
        assert len(gen_issue_id()) == 6

    def test_uses_default_alphabet(self):
        generated = gen_issue_id(issue_id_len=MAX_ISSUE_ID_LEN)
        assert set(generated) <= set(DEFAULT_ID_ALPHABET)

    def test_custom_length(self):
        assert len(gen_issue_id(issue_id_len=10)) == 10

    def test_custom_alphabet(self):
        assert gen_issue_id(issue_id_len=5, alphabet="x") == "xxxxx"

    def test_boundaries_accepted(self):
        assert len(gen_issue_id(issue_id_len=MIN_ISSUE_ID_LEN)) == MIN_ISSUE_ID_LEN
        assert len(gen_issue_id(issue_id_len=MAX_ISSUE_ID_LEN)) == MAX_ISSUE_ID_LEN

    def test_too_short(self):
        with pytest.raises(DDBArgsError, match="too short"):
            gen_issue_id(issue_id_len=MIN_ISSUE_ID_LEN - 1)

    def test_too_long(self):
        with pytest.raises(DDBArgsError, match="too long"):
            gen_issue_id(issue_id_len=MAX_ISSUE_ID_LEN + 1)

    def test_ids_are_not_repeated(self):
        # Not a randomness test, just a guard against a constant return.
        assert len({gen_issue_id() for _ in range(50)}) > 1

    def test_keyword_only(self):
        with pytest.raises(TypeError):
            gen_issue_id(8)


class TestPaginationCursor:
    # Lengths chosen so the encoded payload hits every base64 padding residue
    # (0, 1 and 2 "=" characters before stripping).
    @pytest.mark.parametrize("issue_id", [
        "a",
        "ab",
        "abc",
        "abcd",
        "abcde",
        "abcdef",
        "abcdefg",
        "abcdefgh",
    ])
    def test_round_trip(self, issue_id):
        key = {
            "PK": {"S": f"ISSUE#ENG#{issue_id}"},
            "SK": {"S": "100#INFO"},
        }
        assert decode_pagination_cursor(encode_pagination_cursor(key)) == key

    def test_round_trip_with_gsi_keys(self):
        key = {
            "PK": {"S": "ISSUE#ENG#abc123"},
            "SK": {"S": "100#INFO"},
            "GSI1PK": {"S": "ISSUESPACESTATUS#ENG#TODO"},
            "GSI1SK": {"S": "STATUSUPDATED#2026-01-01T00:00:01.000Z#ISSUE#abc123"},
        }
        assert decode_pagination_cursor(encode_pagination_cursor(key)) == key

    def test_encoded_is_url_safe_and_unpadded(self):
        key = {"PK": {"S": "ISSUE#ENG#abc"}, "SK": {"S": "100#INFO"}}
        cursor = encode_pagination_cursor(key)

        assert isinstance(cursor, str)
        assert "=" not in cursor
        assert "+" not in cursor
        assert "/" not in cursor

    def test_encoded_is_opaque_base64(self):
        key = {"PK": {"S": "ISSUE#ENG#abc"}, "SK": {"S": "100#INFO"}}
        cursor = encode_pagination_cursor(key)

        padded = cursor + "=" * (-len(cursor) % 4)
        assert json.loads(base64.urlsafe_b64decode(padded).decode()) == key

    def test_encode_rejects_unserializable_key(self):
        with pytest.raises(DDBArgsError, match="Invalid exclusive start key"):
            encode_pagination_cursor({"PK": {"B": b"\x00\x01"}})

    def test_encode_rejects_non_json_object(self):
        with pytest.raises(DDBArgsError, match="Invalid exclusive start key"):
            encode_pagination_cursor({"PK": object()})

    # A cursor is handed back by whatever client is paging, so decode is a
    # trust boundary: every way it can fail is one DDBArgsError, never a
    # base64 or JSON error escaping to the caller.
    @pytest.mark.parametrize("cursor", [
        "!!!not-base64!!!",
        "@@@@",
        "",
        "not base64 at all",
    ])
    def test_decode_rejects_a_malformed_cursor(self, cursor):
        with pytest.raises(DDBArgsError, match="Invalid pagination cursor"):
            decode_pagination_cursor(cursor)

    def test_decode_rejects_valid_base64_that_is_not_json(self):
        cursor = base64.urlsafe_b64encode(b"\xff\xfe not json").decode()

        with pytest.raises(DDBArgsError, match="Invalid pagination cursor"):
            decode_pagination_cursor(cursor)

    def test_decode_rejects_a_non_string_cursor(self):
        with pytest.raises(DDBArgsError, match="Invalid pagination cursor"):
            decode_pagination_cursor(None)

    # An ExclusiveStartKey is a flat map of attr name to AttributeValue.
    # Anything else would reach boto3 as a malformed request instead.
    @pytest.mark.parametrize("payload", [
        [{"PK": {"S": "ISSUE#ENG#abc"}}],
        "a bare string",
        42,
        None,
        {"PK": "not an attribute value"},
        {"PK": {"S": "ISSUE#ENG#abc"}, "SK": ["100#INFO"]},
    ])
    def test_decode_rejects_a_key_of_the_wrong_shape(self, payload):
        raw = json.dumps(payload).encode()
        cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")

        with pytest.raises(DDBArgsError, match="Invalid pagination cursor"):
            decode_pagination_cursor(cursor)

    def test_decode_accepts_an_empty_key(self):
        cursor = base64.urlsafe_b64encode(b"{}").decode().rstrip("=")

        assert decode_pagination_cursor(cursor) == {}


class TestValidateIssueStatus:
    """msgspec Structs do not type check on __init__, so nothing below this
    stops a bad status from reaching IssueInfo.status and the GSI1PK it
    composes. That row could then never be read back; see
    TestIssueStatusValidation in test_issue_crud.py."""

    @pytest.mark.parametrize("status", list(IssueStatus))
    def test_accepts_every_member(self, status):
        assert validate_issue_status(status) is status

    @pytest.mark.parametrize("status", ["TODO", "BLOCKED", "IN_PROGRESS",
                                        "DONE"])
    def test_accepts_the_bare_string_form(self, status):
        assert validate_issue_status(status) == IssueStatus(status)

    def test_returns_the_enum_member_not_the_string(self):
        assert isinstance(validate_issue_status("TODO"), IssueStatus)

    @pytest.mark.parametrize("status", [
        "NOT_A_STATUS",
        "todo",
        "",
        "TODO ",
    ])
    def test_rejects_an_unknown_value(self, status):
        with pytest.raises(DDBArgsError, match="Invalid issue status"):
            validate_issue_status(status)

    # Whatever the caller passed, it comes back as a bad argument rather than
    # a TypeError from inside the enum lookup.
    @pytest.mark.parametrize("status", [None, 123, ["TODO"], {"TODO": 1},
                                        object()])
    def test_rejects_a_non_status_type(self, status):
        with pytest.raises(DDBArgsError, match="Invalid issue status"):
            validate_issue_status(status)


class TestRetryOnTransactionConflict:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch):
        """Record sleep durations instead of actually sleeping."""
        slept = []
        monkeypatch.setattr("pl8_base.util.time.sleep", slept.append)
        return slept

    def test_returns_on_first_success(self):
        calls = []

        @retry_on_transaction_conflict()
        def op():
            calls.append(1)
            return "ok"

        assert op() == "ok"
        assert len(calls) == 1

    def test_retries_then_succeeds(self):
        calls = []

        @retry_on_transaction_conflict(attempts=5)
        def op():
            calls.append(1)
            if len(calls) < 3:
                raise DDBTransactionConflictError("conflict")
            return "ok"

        assert op() == "ok"
        assert len(calls) == 3

    def test_reraises_after_attempts_exhausted(self):
        calls = []

        @retry_on_transaction_conflict(attempts=3)
        def op():
            calls.append(1)
            raise DDBTransactionConflictError("conflict")

        with pytest.raises(DDBTransactionConflictError):
            op()

        assert len(calls) == 3

    def test_attempts_one_does_not_retry(self):
        calls = []

        @retry_on_transaction_conflict(attempts=1)
        def op():
            calls.append(1)
            raise DDBTransactionConflictError("conflict")

        with pytest.raises(DDBTransactionConflictError):
            op()

        assert len(calls) == 1

    def test_version_conflict_is_not_retried(self):
        # A stale read does not become fresh by retrying the same request;
        # the caller has to re-read.
        calls = []

        @retry_on_transaction_conflict()
        def op():
            calls.append(1)
            raise DDBVersionConflictError("stale")

        with pytest.raises(DDBVersionConflictError):
            op()

        assert len(calls) == 1

    def test_other_exceptions_are_not_retried(self):
        calls = []

        @retry_on_transaction_conflict()
        def op():
            calls.append(1)
            raise DDBArgsError("bad")

        with pytest.raises(DDBArgsError):
            op()

        assert len(calls) == 1

    def test_rejects_attempts_below_one(self):
        with pytest.raises(DDBArgsError, match="at least 1"):
            retry_on_transaction_conflict(attempts=0)

        with pytest.raises(DDBArgsError, match="at least 1"):
            retry_on_transaction_conflict(attempts=-1)

    def test_sleeps_between_attempts_only(self, no_sleep):
        @retry_on_transaction_conflict(attempts=4)
        def op():
            raise DDBTransactionConflictError("conflict")

        with pytest.raises(DDBTransactionConflictError):
            op()

        # 4 attempts, 3 gaps between them, no sleep after the final failure
        assert len(no_sleep) == 3

    def test_sleep_never_exceeds_max_delay(self, no_sleep, monkeypatch):
        # Draw the top of the full-jitter interval every time, so the cap is
        # what is actually being asserted.
        monkeypatch.setattr("pl8_base.util.random.uniform", lambda a, b: b)

        @retry_on_transaction_conflict(attempts=8, base_delay=0.05, max_delay=1.0)
        def op():
            raise DDBTransactionConflictError("conflict")

        with pytest.raises(DDBTransactionConflictError):
            op()

        assert all(0 <= s <= 1.0 for s in no_sleep)
        # Backoff grows until it hits the cap, then stays there
        assert no_sleep[0] == pytest.approx(0.05)
        assert no_sleep[1] == pytest.approx(0.10)
        assert no_sleep[-1] == pytest.approx(1.0)

    def test_jitter_draws_from_zero(self, monkeypatch):
        # Full jitter, not jitter around a fixed backoff: the lower bound of
        # every draw is 0, which is what keeps conflicting writers from
        # re-colliding in lockstep.
        draws = []

        def record(low, high):
            draws.append((low, high))
            return 0

        monkeypatch.setattr("pl8_base.util.random.uniform", record)

        @retry_on_transaction_conflict(attempts=4)
        def op():
            raise DDBTransactionConflictError("conflict")

        with pytest.raises(DDBTransactionConflictError):
            op()

        assert draws
        assert all(low == 0 for low, _ in draws)

    def test_preserves_wrapped_function_identity(self):
        @retry_on_transaction_conflict()
        def some_operation():
            """Docstring."""
            return None

        assert some_operation.__name__ == "some_operation"
        assert some_operation.__doc__ == "Docstring."

    def test_passes_through_args_and_kwargs(self):
        @retry_on_transaction_conflict()
        def op(a, *, b):
            return (a, b)

        assert op(1, b=2) == (1, 2)


class TestValidateSpaceId:
    def test_accepts_a_plain_id(self):
        validate_space_id("ENG")

    def test_accepts_a_single_character(self):
        validate_space_id("a")

    def test_accepts_digits_hyphens_and_underscores(self):
        validate_space_id("my-space_1")

    def test_accepts_the_maximum_length(self):
        validate_space_id("x" * MAX_SPACE_ID_LEN)

    def test_rejects_an_empty_id(self):
        with pytest.raises(DDBArgsError, match="empty"):
            validate_space_id("")

    def test_rejects_a_hash(self):
        # "#" separates every key group, so a space_id carrying one would make
        # both SPACE#{space_id} and ISSUE#{space_id}#{issue_id} ambiguous.
        with pytest.raises(DDBArgsError, match="invalid characters"):
            validate_space_id("ENG#OPS")

    def test_rejects_whitespace(self):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            validate_space_id("ENG ONE")

    def test_rejects_a_trailing_newline(self):
        # The case a "$" anchored pattern would have let through: "$" matches
        # before a trailing newline, so the newline would land in the key.
        with pytest.raises(DDBArgsError, match="invalid characters"):
            validate_space_id("ENG\n")

    def test_rejects_a_slash(self):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            validate_space_id("ENG/x")

    def test_rejects_an_overlong_id(self):
        with pytest.raises(DDBArgsError, match="too long"):
            validate_space_id("x" * (MAX_SPACE_ID_LEN + 1))

    def test_rejects_a_non_string(self):
        # len() on an int would raise TypeError rather than DDBArgsError.
        with pytest.raises(DDBArgsError, match="must be a string"):
            validate_space_id(123)

    def test_rejects_none(self):
        with pytest.raises(DDBArgsError, match="must be a string"):
            validate_space_id(None)
