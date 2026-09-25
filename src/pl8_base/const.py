# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

MAX_ISSUE_ID_LEN = 16
MIN_ISSUE_ID_LEN = 3
RETRY_ISSUE_ID_COLLISIONS = 5

# Bounded because space_id is caller-supplied and composes both SPACE#{space_id}
# and ISSUE#{space_id}#{issue_id}. 64 leaves the composite key far inside
# DynamoDB's 2048 byte limit.
MAX_SPACE_ID_LEN = 64

# Bounded only to keep a row small. Unlike a space_id a creator never composes
# a key, so its characters are unconstrained; see util.validate_creator.
MAX_CREATOR_LEN = 256

# Full-jitter exponential backoff for TransactionConflict retries
TRANSACT_RETRY_ATTEMPTS = 5
TRANSACT_RETRY_BASE_DELAY = 0.05
TRANSACT_RETRY_MAX_DELAY = 1.0

# CancellationReasons code marking a transient transaction conflict
TRANSACT_CONFLICT_REASON = "TransactionConflict"
# Error code a single-item write raises when a transaction holds its item
TRANSACT_CONFLICT_CODE = "TransactionConflictException"
# CancellationReasons code marking a failed ConditionExpression
CONDITION_FAILED_REASON = "ConditionalCheckFailed"
# Error code a failed ConditionExpression raises outside a transaction
CONDITION_FAILED_CODE = "ConditionalCheckFailedException"

# Name of the single GSI on the base table
GSI1_INDEX_NAME = "GSI1"
