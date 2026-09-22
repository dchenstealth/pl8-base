# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

import gzip

import msgspec
import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

import pl8_base.types
from pl8_base.errors import DDBArgsError
from pl8_base.types import (
    CLASS_MAP,
    EVENT_CLASS_MAP,
    IssueBlocker,
    IssueInfo,
    IssueStatus,
    SpaceInfo,
)
from pl8_base.types.base import BaseObject


@pytest.fixture
def ts():
    return TypeSerializer()


@pytest.fixture
def td():
    return TypeDeserializer()


def make_info(**overrides):
    kwargs = {
        "space_id": "ENG",
        "issue_id": "abc123",
        "title": "test title",
        "description": "test desc",
        "status": IssueStatus.TODO,
    }
    kwargs.update(overrides)
    return IssueInfo(**kwargs)


def make_blocker(**overrides):
    kwargs = {
        "blocking_issue_space_id": "ENG",
        "blocking_issue_id": "aaa111",
        "blocked_issue_space_id": "OPS",
        "blocked_issue_id": "bbb222",
        "is_blocking_issue_done": False,
    }
    kwargs.update(overrides)
    return IssueBlocker(**kwargs)


def make_space(**overrides):
    kwargs = {
        "space_id": "ENG",
        "name": "Engineering",
        "description": "test desc",
    }
    kwargs.update(overrides)
    return SpaceInfo(**kwargs)


class TestIssueStatus:
    def test_members(self):
        assert set(IssueStatus) == {
            IssueStatus.TODO,
            IssueStatus.BLOCKED,
            IssueStatus.IN_PROGRESS,
            IssueStatus.DONE,
        }

    def test_is_a_str_enum(self):
        # Key format strings interpolate status directly, so the rendered value
        # must be the bare name with no "IssueStatus." prefix.
        assert IssueStatus.TODO == "TODO"
        assert f"{IssueStatus.IN_PROGRESS}" == "IN_PROGRESS"


class TestIssueInfoKeys:
    def test_renders_exact_keys(self):
        info = make_info(status_updated_at="2026-01-01T00:00:01.000Z")

        assert info.PK == "ISSUE#ENG#abc123"
        assert info.SK == "100#INFO"
        assert info.GSI1PK == "ISSUESPACESTATUS#ENG#TODO"
        assert info.GSI1SK == "STATUSUPDATED#2026-01-01T00:00:01.000Z#ISSUE#abc123"

    def test_gsi1pk_tracks_status(self):
        info = make_info(status=IssueStatus.IN_PROGRESS)
        assert info.GSI1PK == "ISSUESPACESTATUS#ENG#IN_PROGRESS"

    def test_status_updated_at_defaults_to_created_at(self):
        info = make_info()
        assert info.status_updated_at == info.created_at

    def test_status_updated_at_reaches_gsi1sk(self):
        # IssueInfo.__post_init__ must resolve status_updated_at before
        # BaseObject.__post_init__ renders KEY_ATTRS, or GSI1SK would carry
        # "None".
        info = make_info()
        assert "None" not in info.GSI1SK
        assert info.GSI1SK == f"STATUSUPDATED#{info.status_updated_at}#ISSUE#abc123"

    def test_explicit_status_updated_at_is_used(self):
        info = make_info(status_updated_at="2020-05-05T00:00:00.000Z")
        assert info.GSI1SK == "STATUSUPDATED#2020-05-05T00:00:00.000Z#ISSUE#abc123"
        assert info.status_updated_at != info.created_at

    def test_gsi1sk_sorts_chronologically(self):
        earlier = make_info(status_updated_at="2026-01-01T00:00:01.000Z")
        later = make_info(status_updated_at="2026-01-01T00:00:02.000Z")
        assert earlier.GSI1SK < later.GSI1SK

    def test_preset_keys_are_not_overwritten(self):
        # The load-from-DB path sets key attrs directly; __post_init__ must
        # leave them alone rather than re-render them.
        info = make_info(PK="PRESET#PK", SK="PRESET#SK",
                         GSI1PK="PRESET#GSI1PK", GSI1SK="PRESET#GSI1SK")

        assert info.PK == "PRESET#PK"
        assert info.SK == "PRESET#SK"
        assert info.GSI1PK == "PRESET#GSI1PK"
        assert info.GSI1SK == "PRESET#GSI1SK"


class TestIssueInfoDefaults:
    def test_timestamps_default_and_match(self):
        info = make_info()
        assert info.created_at
        assert info.updated_at == info.created_at

    def test_explicit_timestamps_preserved(self):
        info = make_info(created_at="2020-01-01T00:00:00.000Z",
                         updated_at="2021-01-01T00:00:00.000Z")
        assert info.created_at == "2020-01-01T00:00:00.000Z"
        assert info.updated_at == "2021-01-01T00:00:00.000Z"

    def test_version_starts_at_one(self):
        assert make_info().version == 1

    def test_num_active_blockers_starts_at_zero(self):
        assert make_info().num_active_blockers == 0

    def test_type_version(self):
        assert make_info().type_version == "0.0.1"

    def test_is_done_only_for_done(self):
        assert make_info(status=IssueStatus.DONE).is_done is True
        for status in (IssueStatus.TODO, IssueStatus.BLOCKED,
                       IssueStatus.IN_PROGRESS):
            assert make_info(status=status).is_done is False

    def test_kw_only_enforced(self):
        with pytest.raises(TypeError):
            IssueInfo("ENG", "abc123", "title", "desc", IssueStatus.TODO)


class TestIssueInfoSerialization:
    def test_tags_the_class_name(self, ts):
        item = make_info().serialize(ts=ts)
        assert item["type"] == {"S": "IssueInfo"}

    def test_description_is_compressed(self, ts):
        item = make_info().serialize(ts=ts)

        assert "S" not in item["description"]
        assert "B" in item["description"]
        assert gzip.decompress(item["description"]["B"]).decode() == "test desc"

    def test_title_is_not_compressed(self, ts):
        item = make_info().serialize(ts=ts)
        assert item["title"] == {"S": "test title"}

    def test_keys_serialize_as_strings(self, ts):
        item = make_info(status_updated_at="2026-01-01T00:00:01.000Z").serialize(ts=ts)

        assert item["PK"] == {"S": "ISSUE#ENG#abc123"}
        assert item["SK"] == {"S": "100#INFO"}
        assert item["GSI1PK"] == {"S": "ISSUESPACESTATUS#ENG#TODO"}
        assert item["GSI1SK"] == {
            "S": "STATUSUPDATED#2026-01-01T00:00:01.000Z#ISSUE#abc123",
        }

    def test_status_serializes_as_bare_string(self, ts):
        item = make_info(status=IssueStatus.BLOCKED).serialize(ts=ts)
        assert item["status"] == {"S": "BLOCKED"}

    def test_num_active_blockers_serializes_as_number(self, ts):
        item = make_info(num_active_blockers=3).serialize(ts=ts)
        assert item["num_active_blockers"] == {"N": "3"}

    def test_round_trip(self, ts, td):
        info = make_info(num_active_blockers=2, version=4)
        assert IssueInfo.from_item(info.serialize(ts=ts), td=td) == info

    def test_round_trip_restores_status_enum(self, ts, td):
        loaded = IssueInfo.from_item(make_info().serialize(ts=ts), td=td)
        assert loaded.status is IssueStatus.TODO

    def test_round_trip_restores_int_not_decimal(self, ts, td):
        loaded = IssueInfo.from_item(make_info(num_active_blockers=2).serialize(ts=ts),
                                     td=td)
        assert isinstance(loaded.num_active_blockers, int)
        assert loaded.num_active_blockers == 2

    def test_non_string_compressed_attr_rejected(self, ts):
        info = make_info(description=123)
        with pytest.raises(DDBArgsError, match="Compressed fields must be strings"):
            info.serialize(ts=ts)

    def test_serialized_pk_is_primary_key_only(self, ts):
        assert make_info().serialized_pk(ts=ts) == {
            "PK": {"S": "ISSUE#ENG#abc123"},
            "SK": {"S": "100#INFO"},
        }


class TestIssueBlockerKeys:
    def test_renders_exact_keys(self):
        blocker = make_blocker()

        # The row lives in the blocking Issue's partition...
        assert blocker.PK == "ISSUE#ENG#aaa111"
        assert blocker.SK == "800#BLOCKEDISSUE#OPS#bbb222"
        # ...and GSI1 flips it so the blocked Issue can find its blockers.
        assert blocker.GSI1PK == "BLOCKEDISSUE#OPS#bbb222"
        assert blocker.GSI1SK == "500#BLOCKINGISSUE#ENG#aaa111"

    def test_same_space_keys(self):
        blocker = make_blocker(blocked_issue_space_id="ENG")

        assert blocker.PK == "ISSUE#ENG#aaa111"
        assert blocker.SK == "800#BLOCKEDISSUE#ENG#bbb222"
        assert blocker.GSI1PK == "BLOCKEDISSUE#ENG#bbb222"
        assert blocker.GSI1SK == "500#BLOCKINGISSUE#ENG#aaa111"

    def test_sk_sorts_after_the_info_row(self):
        # get_issue_blocking scans the blocking Issue's partition with
        # begins_with(SK, "800#BLOCKEDISSUE#"); the numeric prefixes are what
        # keep the INFO row out of that range.
        assert IssueInfo.KEY_ATTRS["SK"] < make_blocker().SK
        assert make_blocker().SK.startswith("800#BLOCKEDISSUE#")

    def test_shares_partition_with_the_blocking_issue_info_row(self):
        info = make_info(issue_id="aaa111")
        assert make_blocker().PK == info.PK

    def test_preset_keys_are_not_overwritten(self):
        blocker = make_blocker(PK="PRESET#PK", SK="PRESET#SK",
                               GSI1PK="PRESET#GSI1PK", GSI1SK="PRESET#GSI1SK")

        assert blocker.PK == "PRESET#PK"
        assert blocker.SK == "PRESET#SK"
        assert blocker.GSI1PK == "PRESET#GSI1PK"
        assert blocker.GSI1SK == "PRESET#GSI1SK"

    def test_kw_only_enforced(self):
        with pytest.raises(TypeError):
            IssueBlocker("ENG", "aaa111", "OPS", "bbb222", False)


class TestIssueBlockerSerialization:
    def test_tags_the_class_name(self, ts):
        assert make_blocker().serialize(ts=ts)["type"] == {"S": "IssueBlocker"}

    def test_nothing_is_compressed(self, ts):
        assert IssueBlocker.COMPRESSED_ATTRS == set()

        item = make_blocker().serialize(ts=ts)
        assert all("B" not in v for v in item.values())

    def test_flag_serializes_as_bool(self, ts):
        assert make_blocker().serialize(ts=ts)["is_blocking_issue_done"] == {"BOOL": False}
        assert make_blocker(is_blocking_issue_done=True).serialize(
            ts=ts)["is_blocking_issue_done"] == {"BOOL": True}

    def test_round_trip(self, ts, td):
        blocker = make_blocker()
        assert IssueBlocker.from_item(blocker.serialize(ts=ts), td=td) == blocker

    def test_round_trip_done_flag(self, ts, td):
        blocker = make_blocker(is_blocking_issue_done=True)
        loaded = IssueBlocker.from_item(blocker.serialize(ts=ts), td=td)
        assert loaded.is_blocking_issue_done is True

    def test_serialized_pk_is_primary_key_only(self, ts):
        assert make_blocker().serialized_pk(ts=ts) == {
            "PK": {"S": "ISSUE#ENG#aaa111"},
            "SK": {"S": "800#BLOCKEDISSUE#OPS#bbb222"},
        }


class TestSpaceInfoKeys:
    def test_renders_exact_keys(self):
        space = make_space()

        assert space.PK == "SPACE#ENG"
        assert space.SK == "100#INFO"
        assert space.GSI1PK == "SPACES"
        assert space.GSI1SK == "SPACE#ENG"

    def test_gsi1pk_is_a_constant_bucket(self):
        # Enumeration reads one GSI partition, so GSI1PK must not vary with the
        # space. This is the property get_spaces depends on.
        assert make_space(space_id="ENG").GSI1PK == make_space(
            space_id="OPS").GSI1PK == "SPACES"

    def test_gsi1sk_is_distinct_per_space(self):
        # Cursor-based pagination cannot work with duplicate sort keys.
        assert make_space(space_id="ENG").GSI1SK != make_space(
            space_id="OPS").GSI1SK

    def test_gsi1sk_sorts_alphabetically(self):
        assert make_space(space_id="ALPHA").GSI1SK < make_space(
            space_id="MID").GSI1SK < make_space(space_id="ZED").GSI1SK

    def test_pk_does_not_collide_with_an_issue_partition(self):
        # An Issue in space ENG and the Space ENG itself are different rows.
        assert make_space(space_id="ENG").PK != make_info().PK

    def test_shares_the_info_sort_key_with_an_issue(self):
        # Both are the INFO row of their own partition, so the numeric prefix
        # keeps future space-scoped rows orderable around it.
        assert make_space().SK == IssueInfo.KEY_ATTRS["SK"] == "100#INFO"

    def test_preset_keys_are_not_overwritten(self):
        space = make_space(PK="PRESET#PK", SK="PRESET#SK",
                           GSI1PK="PRESET#GSI1PK", GSI1SK="PRESET#GSI1SK")

        assert space.PK == "PRESET#PK"
        assert space.SK == "PRESET#SK"
        assert space.GSI1PK == "PRESET#GSI1PK"
        assert space.GSI1SK == "PRESET#GSI1SK"

    def test_keyword_only(self):
        with pytest.raises(TypeError):
            SpaceInfo("ENG", "Engineering", "test desc")


class TestSpaceInfoDefaults:
    def test_timestamps_default_together(self):
        space = make_space()
        assert space.created_at
        assert space.updated_at == space.created_at

    def test_version_starts_at_one(self):
        assert make_space().version == 1

    def test_type_version_is_set(self):
        assert make_space().type_version == "0.0.1"


class TestSpaceInfoSerialization:
    def test_row_is_tagged_with_its_type(self, ts):
        assert make_space().serialize(ts=ts)["type"] == {"S": "SpaceInfo"}

    def test_description_is_compressed(self, ts):
        stored = make_space().serialize(ts=ts)["description"]
        assert "S" not in stored
        assert gzip.decompress(stored["B"]).decode() == "test desc"

    def test_name_is_not_compressed(self, ts):
        # Only description is in COMPRESSED_ATTRS; name stays a plain string.
        assert make_space().serialize(ts=ts)["name"] == {"S": "Engineering"}

    def test_round_trip(self, ts, td):
        space = make_space()
        assert SpaceInfo.from_item(space.serialize(ts=ts), td=td) == space

    def test_round_trips_a_multiline_description(self, ts, td):
        space = make_space(description="line one\nline two")
        loaded = SpaceInfo.from_item(space.serialize(ts=ts), td=td)
        assert loaded.description == "line one\nline two"

    def test_serialized_pk_is_primary_key_only(self, ts):
        assert make_space().serialized_pk(ts=ts) == {
            "PK": {"S": "SPACE#ENG"},
            "SK": {"S": "100#INFO"},
        }

    def test_non_string_description_is_rejected(self):
        with pytest.raises(DDBArgsError):
            SpaceInfo.compress_value("description", 123)

    def test_from_item_rejects_a_missing_required_field(self, ts, td):
        item = make_space().serialize(ts=ts)
        del item["name"]

        with pytest.raises(msgspec.ValidationError):
            SpaceInfo.from_item(item, td=td)


class TestClassMap:
    def test_keys_match_class_names(self):
        for name, cls in CLASS_MAP.items():
            assert name == cls.__name__

    def test_covers_every_concrete_base_object(self):
        def concrete_subclasses(cls):
            for sub in cls.__subclasses__():
                yield sub
                yield from concrete_subclasses(sub)

        assert set(concrete_subclasses(BaseObject)) == set(CLASS_MAP.values())

    def test_matches_the_serialized_tag(self, ts):
        for obj in (make_info(), make_blocker(), make_space()):
            tag = obj.serialize(ts=ts)["type"]["S"]
            assert CLASS_MAP[tag] is type(obj)


class TestPublicExports:
    """__all__ is spelled out rather than unpacked from the maps, so that
    type checkers and editors can resolve what the package exports. These
    keep the hand-written list and the maps from drifting apart."""

    def test_every_exported_name_resolves(self):
        for name in pl8_base.types.__all__:
            assert hasattr(pl8_base.types, name), name

    def test_exports_every_mapped_class(self):
        mapped = set(CLASS_MAP) | set(EVENT_CLASS_MAP)

        assert mapped <= set(pl8_base.types.__all__)

    def test_exports_nothing_beyond_the_maps_and_the_named_extras(self):
        extras = {"CLASS_MAP", "EVENT_CLASS_MAP", "BaseEvent", "IssueStatus"}
        mapped = set(CLASS_MAP) | set(EVENT_CLASS_MAP)

        assert set(pl8_base.types.__all__) == mapped | extras

    def test_has_no_duplicates(self):
        assert len(pl8_base.types.__all__) == len(set(pl8_base.types.__all__))


class TestBaseObjectContract:
    def test_dict_is_builtins_only(self):
        as_dict = make_info().dict()
        assert isinstance(as_dict, dict)
        assert isinstance(as_dict["status"], str)
        assert not isinstance(as_dict["status"], IssueStatus)

    def test_dict_includes_the_type_tag(self):
        assert make_info().dict()["type"] == "IssueInfo"

    def test_public_dict_omits_key_attrs(self):
        info = make_info()
        public = info.public_dict()

        for attr in info.KEY_ATTRS:
            assert attr not in public

    def test_public_dict_keeps_non_key_attrs(self):
        info = make_info()
        public = info.public_dict()

        assert public["issue_id"] == info.issue_id
        assert public["title"] == info.title
        assert public["type"] == "IssueInfo"

    def test_public_dict_matches_dict_minus_key_attrs(self):
        info = make_info()
        full = info.dict()
        public = info.public_dict()

        assert public == {k: v for k, v in full.items() if k not in info.KEY_ATTRS}

    def test_from_item_rejects_a_missing_required_field(self, ts, td):
        item = make_info().serialize(ts=ts)
        del item["title"]

        with pytest.raises(msgspec.ValidationError):
            IssueInfo.from_item(item, td=td)

    def test_from_item_defaults_a_type_deserializer(self, ts):
        info = make_info()
        assert IssueInfo.from_item(info.serialize(ts=ts)) == info

    def test_serialize_defaults_a_type_serializer(self):
        assert make_info().serialize()["SK"] == {"S": "100#INFO"}
