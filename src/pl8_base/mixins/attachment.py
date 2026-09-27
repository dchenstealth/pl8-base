# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""IssueAttachment operations.

An attachment is a DynamoDB row plus an S3 object. PL8 never handles the bytes:
callers upload and download them with presigned URLs, and this mixin keeps the
row, its status and the counters in step with them.
"""

import time

from botocore.exceptions import BotoCoreError, ClientError

from ..const import (
    ATTACHMENT_PENDING_TTL_SECONDS,
    ATTACHMENT_SK_PREFIX,
    ATTACHMENT_TTL_ATTR,
    GSI1_INDEX_NAME,
    PRESIGN_EXPIRY_SECONDS,
)
from ..errors import (
    DDBAttachmentStatusError,
    DDBExistsError,
    DDBInternalError,
    DDBMissingError,
    StorageInternalError,
    StorageObjectMissingError,
)
from ..types import AttachmentStatus, IssueAttachment
from ..util import (
    isotime,
    retry_on_transaction_conflict,
    validate_attachment_id,
    validate_attachment_name,
    validate_attachment_size,
    validate_comment_id,
    validate_content_type,
    validate_creator,
    validate_space_id,
)

# HeadObject has no body to carry an error code, so a missing key surfaces as
# "404" rather than the NoSuchKey GetObject raises. All are matched.
OBJECT_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})

# The worst case of delete_attachment_with_counters' ladder: one attempt that
# finds the row confirmed since the read, one per counter row found gone, and
# the final attempt.
ATTACHMENT_DELETE_ATTEMPTS = 4


class AttachmentMixin:
    """IssueAttachment operations."""

    # ------------------------------------------------------------------
    # Key and storage helpers
    # ------------------------------------------------------------------

    def attachment_sk(self, attachment_id):
        return IssueAttachment.KEY_ATTRS["SK"].format(
            attachment_id=attachment_id)

    def attachment_key(self, space_id, issue_id, attachment_id):
        """The serialized primary key of an IssueAttachment row."""
        return {
            "PK": self.ts.serialize(self.issue_pk(space_id, issue_id)),
            "SK": self.ts.serialize(self.attachment_sk(attachment_id)),
        }

    def attachment_s3_key(self, space_id, issue_id, attachment_id):
        """The S3 key an attachment's object lives under.

        Built from the ids rather than a row, so handle_issue_attachment_deleted
        can name the object after the row is gone.
        """
        return IssueAttachment.S3_KEY_FORMAT.format(
            space_id=space_id, issue_id=issue_id, attachment_id=attachment_id)

    def require_storage(self):
        """Refuse an attachment operation on a manager with no storage.

        s3_client and bucket_name are optional on BasePL8, so this reports a
        missing one clearly rather than as an AttributeError on None.

        Raises:
            StorageInternalError: if this manager has no S3 client or bucket
        """
        if self.s3_client is None or self.bucket_name is None:
            raise StorageInternalError(
                "Attachment operations need an s3_client and a bucket_name; "
                "this BasePL8 was constructed without them")

    def log_signing_error(self, exc):
        """Log a failure to sign, from whichever botocore tree it came.

        Signing makes no API call, so its failures are mostly
        NoCredentialsError and NoRegionError. Those are BotoCoreErrors, not
        ClientErrors, and have no exc.response for log_client_error to read.

        Args:
            exc (ClientError or BotoCoreError): the failure to log
        """
        if isinstance(exc, ClientError):
            self.log_client_error(exc)
            return

        self.logger.exception(f"BotoCoreError ({type(exc).__name__})",
                              error=str(exc))

    def presigned_attachment_post(self, attachment):
        """A presigned POST a caller can upload this attachment's bytes with.

        The declared size is signed in as an exact content-length-range, and
        the content type as both a field and a condition, so S3 refuses any
        other upload.

        Args:
            attachment (IssueAttachment): the row to sign an upload for

        Returns:
            dict: as generate_presigned_post returns it, {"url": ...,
                "fields": {...}}, to be POSTed as multipart form data

        Raises:
            StorageInternalError: if this manager has no storage configured, or
                the client cannot sign
        """
        self.require_storage()

        try:
            return self.s3_client.generate_presigned_post(
                Bucket=self.bucket_name,
                Key=attachment.s3_key,
                Fields={"Content-Type": attachment.content_type},
                Conditions=[
                    {"Content-Type": attachment.content_type},
                    ["content-length-range", attachment.size,
                     attachment.size],
                ],
                ExpiresIn=PRESIGN_EXPIRY_SECONDS,
            )
        except (ClientError, BotoCoreError) as exc:
            # See log_signing_error for why both trees.
            self.log_signing_error(exc)
            raise StorageInternalError(
                f"Error signing attachment upload: {exc!s}") from exc

    def presigned_attachment_url(self, attachment):
        """A presigned GET a caller can download this attachment's bytes with.

        Carries the attachment's name in a signed ResponseContentDisposition,
        so the file downloads under its name rather than its id. The name is
        validated so it cannot break out of the header; see
        util.validate_attachment_name.

        Args:
            attachment (IssueAttachment): the row to sign a download for

        Returns:
            str: presigned URL, valid for const.PRESIGN_EXPIRY_SECONDS

        Raises:
            StorageInternalError: if this manager has no storage configured, or
                the client cannot sign
        """
        self.require_storage()

        disposition = f'attachment; filename="{attachment.name}"'

        try:
            return self.s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": attachment.s3_key,
                    "ResponseContentDisposition": disposition,
                },
                ExpiresIn=PRESIGN_EXPIRY_SECONDS,
            )
        except (ClientError, BotoCoreError) as exc:
            # See log_signing_error for why both trees.
            self.log_signing_error(exc)
            raise StorageInternalError(
                f"Error signing attachment download: {exc!s}") from exc

    def head_attachment_object(self, attachment):
        """The S3 metadata of an attachment's object.

        Args:
            attachment (IssueAttachment): the row whose object to head

        Returns:
            dict: the HeadObject response

        Raises:
            StorageObjectMissingError: if the object does not exist
            StorageInternalError: if this manager has no storage configured, or
                the call fails for any other reason
        """
        self.require_storage()

        try:
            return self.s3_client.head_object(Bucket=self.bucket_name,
                                              Key=attachment.s3_key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in OBJECT_MISSING_CODES:
                raise StorageObjectMissingError(
                    "Attachment object not found: "
                    f"{attachment.s3_key}") from exc

            self.log_client_error(exc)
            raise StorageInternalError(
                f"Error reading attachment object: {exc!s}") from exc

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    @retry_on_transaction_conflict()
    def initiate_issue_attachment_upload(self, *, space_id, issue_id, name,
                                         content_type, size, creator,
                                         comment_id=None):
        """Write a PENDING IssueAttachment and sign an upload for it.

        Unlike IssueComment, no counter moves here, so the counter increment
        cannot double as the parent-existence check. The Issue and any named
        IssueComment are checked with plain ConditionChecks instead, and the
        counters move on confirm: a PENDING upload may never happen, so it is
        not counted.

        The row expires after const.ATTACHMENT_PENDING_TTL_SECONDS unless it
        is confirmed.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue being attached to
            name (str): human-readable filename, never part of the S3 key
            content_type (str): media type, signed into the upload
            size (int): exact size in bytes, signed into the upload
            creator (str): who or what is attaching the file, recorded as
                supplied and never verified; see util.validate_creator
            comment_id (str or None): id of the IssueComment to attribute the
                attachment to, or None to attach it to the Issue itself

        Returns:
            tuple: (IssueAttachment, dict) where the dict is the presigned
                POST, as presigned_attachment_post returns it

        Raises:
            DDBArgsError: if space_id, name, content_type, size, creator or
                comment_id is invalid
            DDBMissingError: if the Issue, or the named IssueComment, does not
                exist
            DDBExistsError: if the generated attachment id is already in use
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
            StorageInternalError: if this manager has no storage configured
        """
        validate_space_id(space_id)
        validate_creator(creator)
        validate_attachment_name(name)
        validate_content_type(content_type)
        validate_attachment_size(size)
        if comment_id is not None:
            validate_comment_id(comment_id)

        # Before the write, so a manager with no bucket leaves no PENDING row.
        self.require_storage()

        attachment = IssueAttachment(
            space_id=space_id,
            issue_id=issue_id,
            comment_id=comment_id,
            name=name,
            creator=creator,
            content_type=content_type,
            size=size,
            status=AttachmentStatus.PENDING,
            expires_at=int(time.time()) + ATTACHMENT_PENDING_TTL_SECONDS,
        )

        # Signed before the write, so a signing failure leaves no row behind. A
        # write failing after signing is harmless: the URL is never returned.
        post = self.presigned_attachment_post(attachment)

        # CancellationReasons are positional, and the comment check is only
        # present when a comment was named, hence put_index.
        items = [
            {"ConditionCheck": {
                "TableName": self.table_name,
                "Key": self.issue_info_key(space_id, issue_id),
                "ConditionExpression": "attribute_exists(#PK)",
                "ExpressionAttributeNames": {"#PK": "PK"},
            }},
        ]

        if comment_id is not None:
            items.append({"ConditionCheck": {
                "TableName": self.table_name,
                "Key": self.comment_key(space_id, issue_id, comment_id),
                "ConditionExpression": "attribute_exists(#PK)",
                "ExpressionAttributeNames": {"#PK": "PK"},
            }})

        put_index = len(items)
        items.append({"Put": {
            "TableName": self.table_name,
            "Item": attachment.serialize(ts=self.ts),
            "ConditionExpression": "attribute_not_exists(#PK)",
            "ExpressionAttributeNames": {"#PK": "PK"},
        }})

        try:
            self.dynamodb_client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            self.raise_for_transaction_conflict(exc)

            failed, _ = self.failed_reason_item(exc, 0)
            if failed:
                raise DDBMissingError(
                    f"Issue not found: {space_id}#{issue_id}") from exc

            if comment_id is not None:
                failed, _ = self.failed_reason_item(exc, 1)
                if failed:
                    raise DDBMissingError(
                        "IssueComment not found: "
                        f"{space_id}#{issue_id}#{comment_id}") from exc

            failed, _ = self.failed_reason_item(exc, put_index)
            if failed:
                # Not rerolled: a UUIDv7 clash means ids are not being minted
                # as assumed, as in create_issue_comment.
                raise DDBExistsError(
                    f"IssueAttachment exists: "
                    f"{attachment.attachment_id}") from exc

            self.log_client_error(exc)
            raise DDBInternalError(
                f"Error initiating issue attachment upload: {exc!s}") from exc

        return attachment, post

    def resign_issue_attachment_upload(self, *, space_id, issue_id,
                                       attachment_id):
        """A fresh presigned POST for an IssueAttachment still awaiting bytes.

        For a caller whose upload URL expired between attempts. Signs from the
        row's own size and content type and leaves the row, including its
        expiry, unchanged.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            attachment_id (str): id of the attachment

        Returns:
            tuple: (IssueAttachment, dict), the same shape
                initiate_issue_attachment_upload returns

        Raises:
            DDBArgsError: if space_id is invalid
            DDBMissingError: if the IssueAttachment does not exist
            DDBAttachmentStatusError: if the attachment is not PENDING, so
                there is nothing left to upload
            DDBCorruptedError: if the item cannot be parsed
            DDBInternalError: internal database error
            StorageInternalError: if this manager has no storage configured
        """
        validate_space_id(space_id)

        attachment = self.get_primary_item(
            PK=self.issue_pk(space_id, issue_id),
            SK=self.attachment_sk(attachment_id))

        if attachment.status != AttachmentStatus.PENDING:
            raise DDBAttachmentStatusError(
                "IssueAttachment is not PENDING: "
                f"{space_id}#{issue_id}#{attachment_id}")

        return attachment, self.presigned_attachment_post(attachment)

    @retry_on_transaction_conflict()
    def confirm_issue_attachment_uploaded(self, *, space_id, issue_id,
                                          attachment_id):
        """Mark an IssueAttachment UPLOADED and count it.

        The object is headed first, so confirming before the upload lands
        changes nothing and may be retried. The row takes its size and content
        type from S3 rather than from the caller's declaration.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            attachment_id (str): id of the attachment

        Returns:
            IssueAttachment: the attachment as written

        Raises:
            DDBArgsError: if space_id is invalid
            DDBMissingError: if the IssueAttachment, its Issue, or its linked
                IssueComment does not exist
            DDBAttachmentStatusError: if the attachment is not PENDING, which
                normally means it was already confirmed
            DDBTransactionConflictError: if every attempt conflicts
            DDBCorruptedError: if the item cannot be parsed
            DDBInternalError: internal database error
            StorageObjectMissingError: if the object was never uploaded
            StorageInternalError: if this manager has no storage configured
        """
        validate_space_id(space_id)

        # Read for the comment link and the S3 key, which never change. The
        # status is fenced by the transaction's condition, not by this read.
        attachment = self.get_primary_item(
            PK=self.issue_pk(space_id, issue_id),
            SK=self.attachment_sk(attachment_id))

        head = self.head_attachment_object(attachment)
        size = head["ContentLength"]
        # S3 always reports a ContentType; the fallback only guards a client
        # that omits it.
        content_type = head.get("ContentType") or attachment.content_type

        # Passed in rather than left to _build_update, so it can be returned.
        updated_at = isotime()

        # CancellationReasons are positional. The counter updates are
        # hand-built ADDs that do not bump their owner's version; see
        # issue_num_attachments_update.
        items = [
            {"Update": self._build_update(
                PK=self.issue_pk(space_id, issue_id),
                SK=self.attachment_sk(attachment_id),
                # UPLOADED is terminal, so this condition is also the record
                # that the counters below already moved: a replayed confirm
                # fails it and the whole transaction is a no-op, which keeps
                # the counts exactly-once under at-least-once delivery.
                expected_vals={"status": AttachmentStatus.PENDING},
                remove_attrs=[ATTACHMENT_TTL_ATTR],
                status=AttachmentStatus.UPLOADED,
                size=size,
                content_type=content_type,
                updated_at=updated_at,
            )},
            {"Update": self.issue_num_attachments_update(space_id, issue_id,
                                                         1)},
        ]

        if attachment.comment_id is not None:
            items.append({"Update": self.comment_num_attachments_update(
                space_id, issue_id, attachment.comment_id, 1)})

        try:
            self.dynamodb_client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            self.raise_for_transaction_conflict(exc)

            failed, old = self.failed_reason_item(exc, 0)
            if failed:
                if old is None:
                    raise DDBMissingError(
                        "IssueAttachment not found: "
                        f"{space_id}#{issue_id}#{attachment_id}") from exc
                raise DDBAttachmentStatusError(
                    f"IssueAttachment is {old.status}, not PENDING: "
                    f"{space_id}#{issue_id}#{attachment_id}") from exc

            failed, _ = self.failed_reason_item(exc, 1)
            if failed:
                raise DDBMissingError(
                    f"Issue not found: {space_id}#{issue_id}") from exc

            failed, _ = self.failed_reason_item(exc, 2)
            if failed:
                raise DDBMissingError(
                    "IssueComment not found: "
                    f"{space_id}#{issue_id}#{attachment.comment_id}") from exc

            self.log_client_error(exc)
            raise DDBInternalError(
                f"Error confirming issue attachment: {exc!s}") from exc

        # Built from what was written rather than read back, which could return
        # the pre-write row. Only confirm writes a PENDING row, so the version
        # is the one read plus _build_update's bump.
        attachment.status = AttachmentStatus.UPLOADED
        attachment.size = size
        attachment.content_type = content_type
        attachment.expires_at = None
        attachment.updated_at = updated_at
        attachment.version += 1

        return attachment

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_issue_attachment(self, *, space_id, issue_id, attachment_id):
        """Load one IssueAttachment, with a presigned download URL.

        A PENDING attachment is returned with no URL rather than refused, so a
        caller can see a stuck upload and re-sign or delete it.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            attachment_id (str): id of the attachment

        Returns:
            tuple: (IssueAttachment, str or None) where the str is a presigned
                GET valid for const.PRESIGN_EXPIRY_SECONDS, and None means the
                attachment is not UPLOADED

        Raises:
            DDBArgsError: if space_id is invalid
            DDBMissingError: if the IssueAttachment does not exist
            DDBCorruptedError: if the item cannot be parsed
            DDBInternalError: internal database error
            StorageInternalError: if this manager has no storage configured
        """
        validate_space_id(space_id)

        attachment = self.get_primary_item(
            PK=self.issue_pk(space_id, issue_id),
            SK=self.attachment_sk(attachment_id))

        if not attachment.is_uploaded:
            return attachment, None

        return attachment, self.presigned_attachment_url(attachment)

    def get_issue_attachments(self, *, space_id, issue_id, limit=50,
                              cursor=None, ascending=True):
        """One page of an Issue's IssueAttachments, oldest first by default.

        Sorted by attachment_id, which is creation order. Includes linked and
        unlinked attachments, and PENDING ones, so a caller can find uploads
        that never finished.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            limit (int): maximum rows per page
            cursor (str or None): pagination cursor from a previous page
            ascending (bool): oldest first when True, newest first when False

        Returns:
            tuple: (list[IssueAttachment], str or None)

        Raises:
            DDBArgsError: if space_id or cursor is invalid
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        return self.run_query({
            "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :sk)",
            "ExpressionAttributeNames": {"#pk": "PK", "#sk": "SK"},
            "ExpressionAttributeValues": {
                ":pk": self.ts.serialize(self.issue_pk(space_id, issue_id)),
                ":sk": self.ts.serialize(ATTACHMENT_SK_PREFIX),
            },
            "ScanIndexForward": ascending,
        }, cursor=cursor, limit=limit)

    def get_issue_comment_attachments(self, *, space_id, issue_id, comment_id,
                                      limit=50, cursor=None, ascending=True):
        """One page of the IssueAttachments linked to one IssueComment.

        Read from GSI1, which only linked attachments are in. Eventually
        consistent, so an attachment linked moments ago may be missing; the
        delete sweeps read the partition instead for that reason.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            comment_id (str): id of the comment
            limit (int): maximum rows per page
            cursor (str or None): pagination cursor from a previous page
            ascending (bool): oldest first when True, newest first when False

        Returns:
            tuple: (list[IssueAttachment], str or None)

        Raises:
            DDBArgsError: if space_id or cursor is invalid
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        gsi1pk = IssueAttachment.KEY_ATTRS["GSI1PK"].format(
            space_id=space_id, issue_id=issue_id, comment_id=comment_id)
        return self.run_query({
            "IndexName": GSI1_INDEX_NAME,
            "KeyConditionExpression": "#gsi1pk = :gsi1pk",
            "ExpressionAttributeNames": {"#gsi1pk": "GSI1PK"},
            "ExpressionAttributeValues": {
                ":gsi1pk": self.ts.serialize(gsi1pk),
            },
            "ScanIndexForward": ascending,
        }, cursor=cursor, limit=limit)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_issue_attachment(self, *, space_id, issue_id, attachment_id):
        """Delete an IssueAttachment, decrementing its counters if UPLOADED.

        The S3 object is deleted by handle_issue_attachment_deleted, not here.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            attachment_id (str): id of the attachment

        Raises:
            DDBArgsError: if space_id is invalid
            DDBMissingError: if the IssueAttachment does not exist
            DDBTransactionConflictError: if every attempt conflicts
            DDBCorruptedError: if the item cannot be parsed
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        attachment = self.get_primary_item(
            PK=self.issue_pk(space_id, issue_id),
            SK=self.attachment_sk(attachment_id))

        self.delete_attachment_with_counters(attachment)

    def delete_attachment_with_counters(self, attachment, *, with_issue=True,
                                        with_comment=True):
        """Delete one IssueAttachment row, decrementing if it was counted.

        Shared by delete_issue_attachment and the delete sweeps.

        Every delete is conditioned on the status read, and none is issued
        blind. This is the opposite of delete_blocker_for_sweep: there the
        competing writer makes the decrement itself, so falling through to a
        plain delete is safe. Here the competing writer is confirm, which
        *increments*, so a blind delete of a row confirmed since the read would
        leave the counters permanently too high.

        The attempts form a ladder:

        * UPLOADED: delete with every requested decrement. If a counter's own
          Update is refused, that counter's row is gone; drop just that counter
          and retry, since a missing comment is no excuse to leave the Issue
          overcounting. If the Delete is refused, the row is already gone and
          whoever removed it made the decrements.
        * PENDING: delete with no decrement. If refused, re-read: a missing row
          is done, and a row confirmed since the read goes round again as
          UPLOADED.

        ATTACHMENT_DELETE_ATTEMPTS bounds the ladder exactly; running out means
        an assumption above is wrong, and is reported rather than resolved by a
        blind delete.

        Args:
            attachment (IssueAttachment): the row to delete
            with_issue (bool): decrement the Issue's num_attachments
            with_comment (bool): decrement the comment's, when linked

        Raises:
            DDBTransactionConflictError: if every attempt conflicts
            DDBCorruptedError: if a re-read row cannot be parsed
            DDBInternalError: internal database error, or the ladder was
                exhausted
        """
        for _attempt in range(ATTACHMENT_DELETE_ATTEMPTS):
            if attachment.is_uploaded:
                # The Delete is item 0, so counters start at index 1.
                counters = self.attachment_counter_updates(
                    attachment, -1, with_issue=with_issue,
                    with_comment=with_comment)

                applied, failed_index = self.apply_idempotent_transaction(
                    [{"Delete": self.uploaded_attachment_delete(attachment)},
                     *(entry for _name, entry in counters)],
                    "Error deleting issue attachment")

                if applied or failed_index == 0:
                    return

                if counters[failed_index - 1][0] == "issue":
                    with_issue = False
                else:
                    with_comment = False

                continue

            applied, _failed_index = self.apply_idempotent_transaction(
                [{"Delete": self.pending_attachment_delete(attachment)}],
                "Error deleting issue attachment")

            if applied:
                return

            try:
                attachment = self.get_primary_item(
                    PK=self.issue_pk(attachment.space_id, attachment.issue_id),
                    SK=self.attachment_sk(attachment.attachment_id))
            except DDBMissingError:
                return

        self.logger.error("Gave up deleting an IssueAttachment",
                          space_id=attachment.space_id,
                          issue_id=attachment.issue_id,
                          attachment_id=attachment.attachment_id,
                          status=attachment.status,
                          attempts=ATTACHMENT_DELETE_ATTEMPTS)
        raise DDBInternalError(
            "Error deleting issue attachment: gave up after "
            f"{ATTACHMENT_DELETE_ATTEMPTS} attempts: "
            f"{attachment.space_id}#{attachment.issue_id}"
            f"#{attachment.attachment_id}")

    def uploaded_attachment_delete(self, attachment):
        """Delete dict for an IssueAttachment that must still be UPLOADED."""
        return {
            "TableName": self.table_name,
            "Key": self.attachment_key(attachment.space_id,
                                       attachment.issue_id,
                                       attachment.attachment_id),
            "ConditionExpression":
                "attribute_exists(#PK) AND #status = :uploaded",
            "ExpressionAttributeNames": {"#PK": "PK", "#status": "status"},
            "ExpressionAttributeValues": {
                ":uploaded": self.serialize_value(AttachmentStatus.UPLOADED),
            },
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }

    def pending_attachment_delete(self, attachment):
        """Delete dict for an IssueAttachment that must still be PENDING.

        TTL expiry deletes PENDING rows unconditionally, which is safe because
        confirm removes the expiry in the same transaction as the increment.
        """
        return {
            "TableName": self.table_name,
            "Key": self.attachment_key(attachment.space_id,
                                       attachment.issue_id,
                                       attachment.attachment_id),
            "ConditionExpression":
                "attribute_exists(#PK) AND #status = :pending",
            "ExpressionAttributeNames": {"#PK": "PK", "#status": "status"},
            "ExpressionAttributeValues": {
                ":pending": self.serialize_value(AttachmentStatus.PENDING),
            },
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }

    def attachment_counter_updates(self, attachment, delta, *,
                                   with_issue=True, with_comment=True):
        """The counter updates for one attachment being counted or uncounted.

        The Issue's num_attachments, plus the comment's when linked. with_issue
        and with_comment leave out a counter whose row the caller knows is gone.

        Args:
            attachment (IssueAttachment): the attachment being counted
            delta (int): 1 on confirm, -1 on delete
            with_issue (bool): include the Issue's num_attachments
            with_comment (bool): include the comment's, when linked

        Returns:
            list[tuple]: (name, entry) pairs in transaction order, where entry is
                a transact_write_items entry and name is "issue" or "comment",
                so a cancelled index can be traced back to its counter
        """
        updates = []

        if with_issue:
            updates.append(("issue", {
                "Update": self.issue_num_attachments_update(
                    attachment.space_id, attachment.issue_id, delta)}))

        if with_comment and attachment.comment_id is not None:
            updates.append(("comment", {
                "Update": self.comment_num_attachments_update(
                    attachment.space_id, attachment.issue_id,
                    attachment.comment_id, delta)}))

        return updates

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def handle_issue_comment_deleted(self, *, space_id, issue_id, comment_id):
        """Handle an IssueComment having been deleted.

        Triggered by an SQS event; see the IssueMixin docstring for the path.

        Deletes the comment's linked attachments, decrementing the Issue's
        num_attachments but not the comment's, whose counter went with its row.
        Reads the Issue's partition rather than GSI1, which is eventually
        consistent and could miss a recent link.

        Known limitation: if the Issue was deleted and its issue_id reused
        before this event arrives, the decrements land on the new Issue and can
        drive its counter negative. Accepted along with the stranded rows
        handle_issue_deleted already accepts in that window; the fix belongs in
        issue_id generation, not here.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            comment_id (str): id of the deleted comment

        Raises:
            DDBArgsError: if space_id is invalid
            DDBTransactionConflictError: if every attempt conflicts
            DDBInternalError: internal database error
        """
        validate_space_id(space_id)

        for item in self.paginate(self.get_issue_partition, space_id=space_id,
                                  issue_id=issue_id):
            # Matched positively, so a row type added later is skipped rather
            # than swept.
            if (isinstance(item, IssueAttachment)
                    and item.comment_id == comment_id):
                self.delete_attachment_with_counters(item, with_comment=False)

    def handle_issue_attachment_deleted(self, *, space_id, issue_id,
                                        attachment_id):
        """Handle an IssueAttachment row having been deleted.

        Triggered by an SQS event; see the IssueMixin docstring for the path.

        Deletes the S3 object. Every way a row is removed, including TTL
        expiry, reaches here, which is why objects are deleted here and nowhere
        else. DeleteObject succeeds whether or not the key exists, so this is
        idempotent.

        The key is rebuilt from validated ids rather than taken from the event,
        so an event cannot name an object that is not an attachment.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            attachment_id (str): id of the deleted attachment

        Raises:
            DDBArgsError: if space_id or attachment_id is invalid
            StorageInternalError: if this manager has no storage configured, or
                the delete fails
        """
        validate_space_id(space_id)
        validate_attachment_id(attachment_id)
        self.require_storage()

        key = self.attachment_s3_key(space_id, issue_id, attachment_id)

        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=key)
        except ClientError as exc:
            # Raised rather than swallowed, so the event is retried or lands
            # in the DLQ instead of leaving the bytes behind.
            self.log_client_error(exc)
            raise StorageInternalError(
                f"Error deleting attachment object: {exc!s}") from exc
