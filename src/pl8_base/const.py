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

# Upper bound on an IssueAttachment's declared size. The presigned POST is
# signed with a content-length-range condition built from it, so S3 itself
# refuses a body that disagrees; this bound is what keeps a caller from having
# a 5 GB object signed for in the first place.
MAX_ATTACHMENT_SIZE_BYTES = 100 * 1024 * 1024

# Bounded because the name is interpolated into the signed
# ResponseContentDisposition of a presigned download URL, and because it rides
# in the row rather than in the S3 key; see util.validate_attachment_name.
MAX_ATTACHMENT_NAME_LEN = 128

# Bounded for the same reason: the content type becomes a signed condition in
# the presigned POST policy; see util.validate_content_type.
MAX_CONTENT_TYPE_LEN = 128

# How long a PENDING IssueAttachment row is allowed to sit before DynamoDB's
# TTL reaps it. An initiated upload that is never confirmed would otherwise be
# an orphan row forever: nothing else deletes it, since the caller that would
# have confirmed it is the one that went away. A day is long enough that a
# caller retrying a large upload across expired URLs (see
# resign_issue_attachment_upload) is never reaped mid-retry.
#
# Reaping a PENDING row is safe precisely because a PENDING attachment is not
# counted: confirm is what moves num_attachments, so a row removed before it
# lands leaves no counter behind. TTL deletion is an ordinary delete as far as
# the stream is concerned, so IssueAttachmentDeleted still cleans up whatever
# partial object S3 holds.
ATTACHMENT_PENDING_TTL_SECONDS = 86400

# Lifetime of every presigned URL and POST minted here. Short on purpose: the
# URL is a bearer credential for one object, so it is cheaper to re-sign (no
# API call is made to sign) than to hand out a long-lived one.
PRESIGN_EXPIRY_SECONDS = 300

# Sort key prefix of an IssueAttachment row. 600 sits between IssueComment's
# 500 and IssueBlocker's 800, so a bare PK query on an Issue's partition
# returns its info row, then its comments, then its attachments, then its
# blockers. Leaving gaps between the groups is what lets a row type be added
# later without renumbering the ones already stored; see
# dchenstealth/docs guidelines/dynamodb_keys.md.
ATTACHMENT_SK_PREFIX = "600#ATTACHMENT#"

# Attribute DynamoDB's TTL is configured against on the base table. Only an
# IssueAttachment ever carries it, and only while it is PENDING: confirm
# removes the attribute rather than setting it to a later time, so a confirmed
# attachment has no expiry to be reaped by.
ATTACHMENT_TTL_ATTR = "expires_at"
