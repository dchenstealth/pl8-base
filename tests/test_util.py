import base64
import json

from datetime import datetime, timedelta, timezone, UTC
from decimal import Decimal

import pytest

from pl8_base.const import (
    MAX_ISSUE_ID_LEN,
    MIN_ISSUE_ID_LEN,
)
from pl8_base.errors import (
    DDBArgsError,
    DDBTransactionConflictError,
    DDBVersionConflictError,
)
from pl8_base.util import (
    DEFAULT_ID_ALPHABET,
    cleanup_decimals,
    decode_pagination_cursor,
    encode_pagination_cursor,
    gen_issue_id,
    isotime,
    retry_on_transaction_conflict,
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
