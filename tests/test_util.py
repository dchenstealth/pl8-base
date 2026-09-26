# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pl8_base.const import (
    MAX_ATTACHMENT_NAME_LEN,
    MAX_ATTACHMENT_SIZE_BYTES,
    MAX_CONTENT_TYPE_LEN,
    MAX_CREATOR_LEN,
    MAX_ISSUE_ID_LEN,
    MAX_SPACE_ID_LEN,
    MIN_ISSUE_ID_LEN,
)
from pl8_base.errors import (
    DDBArgsError,
    DDBTransactionConflictError,
    DDBVersionConflictError,
)
from pl8_base.types import AttachmentStatus, IssueStatus
from pl8_base.util import (
    DEFAULT_ID_ALPHABET,
    cleanup_decimals,
    decode_pagination_cursor,
    encode_pagination_cursor,
    gen_issue_id,
    isotime,
    isotime_from_uuid7,
    retry_on_transaction_conflict,
    validate_attachment_name,
    validate_attachment_size,
    validate_attachment_status,
    validate_comment_id,
    validate_content_type,
    validate_creator,
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

        parsed = datetime.fromisoformat(result)
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

    def test_does_not_mutate_the_input(self):
        original = {"a": Decimal("1"), "b": {"c": Decimal("2.5")},
                    "d": [Decimal("3")]}
        result = cleanup_decimals(original)

        assert original == {"a": Decimal("1"), "b": {"c": Decimal("2.5")},
                            "d": [Decimal("3")]}
        assert result is not original
        assert result["b"] is not original["b"]
        assert result["d"] is not original["d"]

    def test_empty_containers_round_trip(self):
        assert cleanup_decimals({}) == {}
        assert cleanup_decimals([]) == []


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
            return

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


class TestIsotimeFromUuid7:
    def test_reads_back_the_minting_instant(self):
        before = isotime()
        comment_id = str(uuid.uuid7())
        after = isotime()

        assert before <= isotime_from_uuid7(comment_id) <= after

    def test_matches_isotime_formatting(self):
        # created_at is stored and compared as a string, so the format has to
        # be the one every other timestamp uses.
        created_at = isotime_from_uuid7(str(uuid.uuid7()))

        assert created_at.endswith("Z")
        assert isotime(datetime.fromisoformat(created_at)) == created_at

    def test_round_trips_a_known_timestamp(self):
        # RFC 9562 puts unix_ts_ms in the leading 48 bits
        unix_ts_ms = 1790251200123
        comment_id = str(uuid.UUID(int=(unix_ts_ms << 80) | (0x7 << 76)
                                   | (0b10 << 62)))

        assert isotime_from_uuid7(comment_id) == "2026-09-24T12:00:00.123Z"

    def test_rejects_a_malformed_id(self):
        with pytest.raises(DDBArgsError, match="Invalid UUID"):
            isotime_from_uuid7("not a uuid")

    def test_rejects_a_non_string(self):
        with pytest.raises(DDBArgsError, match="Invalid UUID"):
            isotime_from_uuid7(None)

    def test_rejects_a_uuid_of_another_version(self):
        # A v4 carries no timestamp, so there is nothing to read back.
        with pytest.raises(DDBArgsError, match="Not a UUIDv7"):
            isotime_from_uuid7(str(uuid.uuid4()))


class TestValidateCreator:
    def test_accepts_a_plain_creator(self):
        validate_creator("alice")

    def test_accepts_the_maximum_length(self):
        validate_creator("x" * MAX_CREATOR_LEN)

    def test_accepts_characters_a_space_id_may_not_use(self):
        # A creator never composes a key, so nothing here needs excluding.
        validate_creator("agent:claude #1 <bot@example.com>")

    def test_rejects_an_empty_creator(self):
        with pytest.raises(DDBArgsError, match="empty"):
            validate_creator("")

    def test_rejects_a_non_string(self):
        with pytest.raises(DDBArgsError, match="must be a string"):
            validate_creator(1)

    def test_rejects_too_long(self):
        with pytest.raises(DDBArgsError, match="too long"):
            validate_creator("x" * (MAX_CREATOR_LEN + 1))


class TestValidateAttachmentStatus:
    """The same reasoning as validate_issue_status: msgspec Structs do not type
    check on __init__, so a bad status would reach the row and make it
    unreadable."""

    @pytest.mark.parametrize("status", list(AttachmentStatus))
    def test_accepts_every_member(self, status):
        assert validate_attachment_status(status) is status

    @pytest.mark.parametrize("status", ["PENDING", "UPLOADED"])
    def test_accepts_the_bare_string_form(self, status):
        assert validate_attachment_status(status) == AttachmentStatus(status)

    def test_returns_the_enum_member_not_the_string(self):
        assert isinstance(validate_attachment_status("PENDING"),
                          AttachmentStatus)

    @pytest.mark.parametrize("status", ["NOT_A_STATUS", "pending", "",
                                       "PENDING "])
    def test_rejects_an_unknown_value(self, status):
        with pytest.raises(DDBArgsError, match="Invalid attachment status"):
            validate_attachment_status(status)

    @pytest.mark.parametrize("status", [None, 123, ["PENDING"], object()])
    def test_rejects_a_non_status_type(self, status):
        with pytest.raises(DDBArgsError, match="Invalid attachment status"):
            validate_attachment_status(status)


class TestValidateCommentId:
    """A comment id composes a sort key twice over, so it is checked by the one
    rule PL8 has for comment ids: it is a UUIDv7."""

    def test_accepts_a_uuidv7(self):
        validate_comment_id(str(uuid.uuid7()))

    def test_rejects_a_uuid4(self):
        with pytest.raises(DDBArgsError, match="Not a UUIDv7"):
            validate_comment_id(str(uuid.uuid4()))

    @pytest.mark.parametrize("comment_id", [
        "",
        "nosuch",
        "500#COMMENT#x",
        "0199f3a1-0000-7000-8000-00000000000",
        None,
        123,
    ])
    def test_rejects_anything_that_is_not_one(self, comment_id):
        with pytest.raises(DDBArgsError):
            validate_comment_id(comment_id)

    def test_a_separator_cannot_reach_a_key(self):
        # The point of the check: an id carrying a "#" would move the sort key
        # it composes rather than fail to match it.
        with pytest.raises(DDBArgsError):
            validate_comment_id("abc#def")


class TestValidateAttachmentName:
    def test_accepts_a_plain_filename(self):
        validate_attachment_name("report.pdf")

    def test_accepts_spaces_and_punctuation(self):
        # A name never composes a key, so a filename may look like a filename.
        validate_attachment_name("Q3 report (final), v2.pdf")

    def test_accepts_non_ascii(self):
        validate_attachment_name("réunion.pdf")

    def test_accepts_the_maximum_length(self):
        validate_attachment_name("x" * MAX_ATTACHMENT_NAME_LEN)

    def test_rejects_an_empty_name(self):
        with pytest.raises(DDBArgsError, match="empty"):
            validate_attachment_name("")

    def test_rejects_a_non_string(self):
        with pytest.raises(DDBArgsError, match="must be a string"):
            validate_attachment_name(1)

    def test_rejects_too_long(self):
        with pytest.raises(DDBArgsError, match="too long"):
            validate_attachment_name("x" * (MAX_ATTACHMENT_NAME_LEN + 1))

    @pytest.mark.parametrize("name", [
        # The name is interpolated into a signed
        # `attachment; filename="<name>"` header on the download URL, so each
        # of these is an injection into a header the caller does not otherwise
        # control.
        'quote".pdf',
        "back\\slash.pdf",
        "new\nline.pdf",
        "carriage\rreturn.pdf",
        "null\x00byte.pdf",
        "tab\tstop.pdf",
        "delete\x7f.pdf",
    ])
    def test_rejects_characters_that_break_out_of_the_header(self, name):
        with pytest.raises(DDBArgsError, match="invalid characters"):
            validate_attachment_name(name)


class TestValidateContentType:
    @pytest.mark.parametrize("content_type", [
        "application/pdf",
        "text/plain",
        "image/svg+xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "x-custom/x.future-format",
    ])
    def test_accepts_a_media_type(self, content_type):
        # Checked as a shape, not against a list: PL8 has no opinion on the
        # format, so an allowlist would only reject next year's media types.
        validate_content_type(content_type)

    def test_accepts_the_maximum_length(self):
        validate_content_type("a/" + "x" * (MAX_CONTENT_TYPE_LEN - 2))

    def test_rejects_an_empty_type(self):
        with pytest.raises(DDBArgsError, match="empty"):
            validate_content_type("")

    def test_rejects_a_non_string(self):
        with pytest.raises(DDBArgsError, match="must be a string"):
            validate_content_type(1)

    def test_rejects_too_long(self):
        with pytest.raises(DDBArgsError, match="too long"):
            validate_content_type("a/" + "x" * MAX_CONTENT_TYPE_LEN)

    @pytest.mark.parametrize("content_type", [
        "application",
        "application/",
        "/pdf",
        "application//pdf",
        "application/pdf/extra",
        "application pdf",
        "application/pdf\n",
        # Parameters are refused on purpose: the type is signed into the policy
        # as an exact condition, so a caller sending a parameterized type
        # against a policy signed without one would be refused by S3 instead.
        "text/plain; charset=utf-8",
    ])
    def test_rejects_anything_that_is_not_type_subtype(self, content_type):
        with pytest.raises(DDBArgsError, match="Invalid content type"):
            validate_content_type(content_type)


class TestValidateAttachmentSize:
    @pytest.mark.parametrize("size", [1, 1024, MAX_ATTACHMENT_SIZE_BYTES])
    def test_accepts_a_size_in_range(self, size):
        validate_attachment_size(size)

    def test_rejects_zero(self):
        with pytest.raises(DDBArgsError, match="at least 1 byte"):
            validate_attachment_size(0)

    def test_rejects_a_negative_size(self):
        with pytest.raises(DDBArgsError, match="at least 1 byte"):
            validate_attachment_size(-1)

    def test_rejects_too_large(self):
        with pytest.raises(DDBArgsError, match="too large"):
            validate_attachment_size(MAX_ATTACHMENT_SIZE_BYTES + 1)

    @pytest.mark.parametrize("size", ["11", 11.0, None, [11]])
    def test_rejects_a_non_integer(self, size):
        with pytest.raises(DDBArgsError, match="must be an integer"):
            validate_attachment_size(size)

    def test_rejects_a_bool(self):
        # True is an int in Python, so it would otherwise be a legal one byte.
        with pytest.raises(DDBArgsError, match="must be an integer"):
            validate_attachment_size(True)
