# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""Shared fixtures for the pl8-base suite.

Tests arrange their state through the real manager methods rather than writing
rows directly, so each one asserts against the rows the implementation actually
writes rather than against a second, hand-written account of what a row should
look like. test_manager.py's put_raw is the deliberate exception: it exists for
the corruption cases the API cannot produce.

Only cross-entity fixtures belong here. Anything specific to one entity stays
local to its own test module, and in particular nothing here may write a row:
several tests assert exact table contents through scan_all(), so a fixture that
created, say, a Space would silently break the Issue counts.
"""

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
import pytest
from aws_lambda_powertools import Logger
from moto import mock_aws

from pl8_base.manager import BasePL8


@pytest.fixture
def aws_environment(monkeypatch):
    """Fake credentials so a misconfigured test cannot reach real AWS."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def mocked_aws(aws_environment):
    with mock_aws():
        yield


@pytest.fixture
def ctv():
    """Common test values."""
    return SimpleNamespace(
        table_name="test-base-table",
        region="us-east-1",
        space_id="ENG",
        other_space_id="OPS",
    )


@pytest.fixture
def dynamodb_client(ctv, mocked_aws):
    client = boto3.client("dynamodb", region_name=ctv.region)
    client.create_table(
        TableName=ctv.table_name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "GSI1",
            "KeySchema": [
                {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
    )
    yield client


@pytest.fixture
def logger():
    """Powertools logger, not stdlib.

    manager.parse_item calls self.logger.error(msg, item=item, ...), passing
    arbitrary kwargs to be merged into the log record. A stdlib logging.Logger
    raises TypeError on those.
    """
    yield Logger(service="pl8-base-test", level="DEBUG")


@pytest.fixture
def mgr(ctv, dynamodb_client, logger):
    yield BasePL8(dynamodb_client=dynamodb_client,
                  table_name=ctv.table_name,
                  logger=logger)


@pytest.fixture
def frozen_clock(monkeypatch):
    """Replace isotime with a fake that advances one second per call.

    util.isotime is imported by name into types/base.py and types/issue.py, so
    patching pl8_base.util alone would not affect them. Patch every pl8_base
    module that carries the name, so new import sites are covered too.

    Anchored at the real current time rather than a fixed date, so rows written
    before this fixture activates still sort before the ones written after it.
    A fixed past date would make an Issue created by an earlier fixture look
    newer than everything the test goes on to write.

    Yields a controller with .peek() for the last value handed out and .tick()
    to skip ahead, so tests can pin the timestamps that land in GSI1SK.
    """
    state = SimpleNamespace(seconds=0)
    anchor = datetime.now(tz=UTC)

    import pl8_base.util as util_module
    real_isotime = util_module.isotime

    def at(seconds):
        return real_isotime(anchor + timedelta(seconds=seconds))

    def fake_isotime(dt=None, timespec="milliseconds"):
        if dt is not None:
            # Defer to the real implementation for explicit datetimes
            return real_isotime(dt=dt, timespec=timespec)

        state.seconds += 1
        return at(state.seconds)

    for name, module in list(sys.modules.items()):
        if not name.startswith("pl8_base"):
            continue
        if getattr(module, "isotime", None) is real_isotime:
            monkeypatch.setattr(module, "isotime", fake_isotime)

    yield SimpleNamespace(
        peek=lambda: at(state.seconds),
        tick=lambda n=1: setattr(state, "seconds", state.seconds + n),
    )


@pytest.fixture
def get_raw(ctv, dynamodb_client):
    """Read one row as raw DynamoDB AttributeValues, or None if absent.

    Read-only on purpose: tests assert against the rows the implementation
    wrote, rather than against a second hand-written definition of what a row
    should look like.
    """
    def _get_raw(PK, SK):
        resp = dynamodb_client.get_item(
            TableName=ctv.table_name,
            Key={"PK": {"S": PK}, "SK": {"S": SK}},
        )
        return resp.get("Item")

    return _get_raw


@pytest.fixture
def scan_all(ctv, dynamodb_client):
    """Every row in the table, for "nothing else was written" assertions."""
    def _scan_all():
        items = []
        params = {"TableName": ctv.table_name}
        while True:
            resp = dynamodb_client.scan(**params)
            items.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                return items
            params["ExclusiveStartKey"] = last_key

    return _scan_all
