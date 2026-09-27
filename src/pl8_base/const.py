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

# Upper bound on an IssueAttachment's declared size, checked before an upload
# is signed.
MAX_ATTACHMENT_SIZE_BYTES = 100 * 1024 * 1024

# Bounded because the name is signed into every download URL; see
# util.validate_attachment_name.
MAX_ATTACHMENT_NAME_LEN = 128

# Bounded because the content type is stored on the S3 object and served back
# on every download; see util.validate_content_type.
MAX_CONTENT_TYPE_LEN = 128

# How long a PENDING IssueAttachment lives before DynamoDB's TTL deletes it,
# counted from creation and never extended. Nothing else would ever delete an
# upload that was started and never confirmed. Safe because PENDING rows are
# not counted, and the delete still emits IssueAttachmentDeleted, which removes
# any object that was uploaded.
ATTACHMENT_PENDING_TTL_SECONDS = 86400

# Lifetime of every presigned URL and POST. Short because a presigned URL is a
# bearer credential, and re-signing is cheap.
PRESIGN_EXPIRY_SECONDS = 300

# Sort key prefix of an IssueAttachment row, between IssueComment's 500 and
# IssueBlocker's 800; see dchenstealth/docs guidelines/dynamodb_keys.md.
ATTACHMENT_SK_PREFIX = "600#ATTACHMENT#"

# Attribute DynamoDB's TTL is configured against. Only a PENDING
# IssueAttachment carries it; confirm removes it.
ATTACHMENT_TTL_ATTR = "expires_at"
