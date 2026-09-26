# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""IssueAttachment operations.

An attachment is two things that must agree: a row in DynamoDB and an object in
S3. The row is written first, because it is what the presigned POST is signed
against, and AttachmentStatus is which of the two is true yet. PL8 never
handles the bytes: a caller uploads them to S3 with a presigned POST and
downloads them with a presigned GET, and everything here is the bookkeeping
around those two URLs.

The invariants, and where each is held:

* An attachment's row and object MUST NOT outlive each other. The row is the
  record of the object, so it goes first and the object follows: deleting the
  row emits IssueAttachmentDeleted, and handle_issue_attachment_deleted deletes
  the object. An object with no row is therefore only ever in flight, and a
  PENDING row's expiry (const.ATTACHMENT_PENDING_TTL_SECONDS) is what keeps an
  upload that never completed from being a row forever.

* An IssueAttachment MUST NOT outlive its Issue, and a linked one MUST NOT
  outlive its IssueComment. IssueMixin.handle_issue_deleted sweeps the Issue's
  partition, handle_issue_comment_deleted below sweeps one comment's
  attachments, and both are driven by events off the row deletes.

* IssueInfo.num_attachments, and IssueComment.num_attachments for a linked
  attachment, MUST count exactly the UPLOADED attachments. The whole argument
  for that is the one-way status transition: confirm conditions its write on
  status = PENDING and moves the counters in the same transaction, so a
  replayed confirm re-fails the condition and the transaction is a no-op, while
  delete conditions its decrement on status = UPLOADED so a PENDING attachment,
  which was never counted, never decrements. See types/enums.py
  AttachmentStatus.

  This is a deliberate departure from the idiom CommentMixin and SpaceMixin
  establish, where the counter increment IS the parent-existence fence, applied
  in the same transaction as the child row's Put. Anyone who reads those first
  will assume it still holds here, and it does not: initiate writes the
  attachment row with plain ConditionChecks on the parents and moves no
  counter, because the row it writes describes an upload that may never happen.
  The parents are fenced twice instead, once at initiate and again at confirm,
  and the counters move only at confirm.

Every public method validates its space_id before it reaches a key, the same as
IssueMixin, CommentMixin and SpaceMixin do, and every method that signs a URL
first checks that this manager was given something to sign against; see
require_storage.
"""

import time

from botocore.exceptions import ClientError

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
    validate_attachment_name,
    validate_attachment_size,
    validate_comment_id,
    validate_content_type,
    validate_creator,
    validate_space_id,
)

# Error code S3 reports for a HeadObject against a key that is not there.
# HeadObject has no response body to put an error code in, so S3 answers with a
# bare 404 and botocore surfaces the status as the code; it is NOT the NoSuchKey
# a GetObject would raise. Both are matched anyway, so that a client or a
# stand-in that does report NoSuchKey is read the same way.
OBJECT_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class AttachmentMixin:
    """IssueAttachment operations; see this module's docstring for the
    invariants and where each is held."""

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

        Recomputed from the ids rather than read off a row, so a caller that no
        longer has the row, handle_issue_attachment_deleted in particular, can
        still name the object. Shares IssueAttachment's format string, so the
        layout is still defined in exactly one place.
        """
        return IssueAttachment.S3_KEY_FORMAT.format(
            space_id=space_id, issue_id=issue_id, attachment_id=attachment_id)

    def require_storage(self):
        """Refuse an attachment operation on a manager with no storage.

        s3_client and bucket_name are optional on BasePL8 so that consumers
        which never touch attachments need not configure S3; see
        BasePL8.__init__. That makes this the place where a manager missing
        them is reported, rather than as an AttributeError on None or a boto3
        complaint about a bucket named None several frames deeper.

        Raises:
            StorageInternalError: if this manager has no S3 client or bucket
        """
        if self.s3_client is None or self.bucket_name is None:
            raise StorageInternalError(
                "Attachment operations need an s3_client and a bucket_name; "
                "this BasePL8 was constructed without them")

    def presigned_attachment_post(self, attachment):
        """A presigned POST a caller can upload this attachment's bytes with.

        The size is signed in as an exact content-length-range, low and high
        both the declared size, because the caller declared that size when it
        initiated the upload. S3 then refuses any other body length, which is
        what makes IssueAttachment.size S3's fact as well as the caller's claim
        even before confirm reads it back off the object. Content-Type is
        signed in the same way, as both a field and a condition.

        Signing is local: it derives a signature from the credentials this
        client already holds and makes no call to S3, so this is cheap and a
        URL may be minted for an object that does not exist yet, which is the
        entire point of it.

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
        except ClientError as exc:
            # Not a failed API call, since signing makes none: a client that
            # cannot resolve credentials or a region to sign with.
            self.log_client_error(exc)
            raise StorageInternalError(
                f"Error signing attachment upload: {exc!s}") from exc

    def presigned_attachment_url(self, attachment):
        """A presigned GET a caller can download this attachment's bytes with.

        Carries the attachment's name as a signed ResponseContentDisposition,
        which is how the human-readable filename reaches the downloader without
        `name` ever entering the S3 key: S3 echoes the header back on the
        response, so the browser saves the file under the name the caller gave
        rather than under an opaque uuid. Signed, so a holder of the URL cannot
        rewrite the name; validated on the way in, so the name cannot break out
        of the quoted header value it is interpolated into; see
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
        except ClientError as exc:
            self.log_client_error(exc)
            raise StorageInternalError(
                f"Error signing attachment download: {exc!s}") from exc

    def head_attachment_object(self, attachment):
        """The S3 metadata of an attachment's object.

        What confirm asks S3 for rather than trusting the caller: the size and
        content type here are properties of the bytes that actually landed.

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

        The row exists before the bytes do, because it is what the presigned
        POST is signed against: its attachment_id is the S3 key, and the size
        and content type it records are the conditions the upload is signed
        with. Nothing is attached to the Issue as far as any reader is
        concerned until confirm_issue_attachment_uploaded lands.

        No counter moves here, which is worth stopping on: it is a deliberate
        departure from the idiom CommentMixin and SpaceMixin establish, where
        the counter increment on the parent row IS the parent-existence fence
        and is applied in the same transaction as the child's Put. An upload
        that is only authorized must not be counted, so the parents are fenced
        with plain ConditionChecks instead and the counters move on confirm.
        The parents are therefore checked twice, once here and once there, and
        the second check is the one the counters hang off.

        The Issue and, when given, the IssueComment are checked in the same
        transaction as the Put so the row cannot be written into a partition no
        Issue owns, or be linked to a comment that is not there.

        The row carries an expires_at: a caller that never uploads or never
        confirms would otherwise leave a row behind forever, since it is the
        only party that knew about it. See
        const.ATTACHMENT_PENDING_TTL_SECONDS.

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

        # Checked before the row is written, not after: a manager with no
        # bucket can never produce a usable attachment, so it must not leave a
        # PENDING row behind for the TTL to clean up either.
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

        # Ordering is load-bearing: CancellationReasons come back positionally.
        # The comment check is only present when a comment was named, so the
        # Put is item 1 for an unlinked attachment and item 2 for a linked one;
        # put_index below is what keeps the reasons read against the right
        # items.
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
                # Not rerolled into a new id, for the same reason
                # create_issue_comment does not reroll: a UUIDv7 clash is not
                # contention to retry past but a sign that ids are not being
                # minted as assumed.
                raise DDBExistsError(
                    f"IssueAttachment exists: "
                    f"{attachment.attachment_id}") from exc

            self.log_client_error(exc)
            raise DDBInternalError(
                f"Error initiating issue attachment upload: {exc!s}") from exc

        return attachment, self.presigned_attachment_post(attachment)

    def resign_issue_attachment_upload(self, *, space_id, issue_id,
                                       attachment_id):
        """A fresh presigned POST for an IssueAttachment still awaiting bytes.

        A presigned POST lasts const.PRESIGN_EXPIRY_SECONDS, which a large
        upload on a slow link can outlast, and a caller whose URL expired
        mid-retry has a perfectly good PENDING row it should keep using.
        Without this it would have to initiate again, stranding the first row
        for the TTL to reap and having already been charged for whatever bytes
        the abandoned upload transferred.

        Re-signs from the row's own size and content_type rather than from
        anything passed in. Those are what the eventual confirm compares
        against and what the original POST was signed with, so re-signing
        against new values would quietly change the terms of the upload, and an
        attachment's declared size is fixed at initiate.

        The status is deliberately not moved: the row stays PENDING, which is
        the state that lets the eventual confirm count it exactly once.

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

        The object is headed before anything is written, so a confirm that
        arrives before the upload finished leaves the row PENDING, the counters
        untouched, and the caller free to upload and confirm again.

        size and content_type are written back from the HEAD response, so the
        row records what S3 actually holds rather than what the caller claimed
        at initiate. The presigned POST's conditions mean the two normally
        agree; this is what makes that a fact about the row rather than an
        assumption about the upload.

        The `status = PENDING` condition on the attachment is the entire
        correctness argument for the counters. Both this call and the event
        paths around it are at-least-once, so a replayed confirm must not
        increment twice: it cannot, because the status has already moved and
        DynamoDB cancels the whole transaction on that condition, counters
        included. The increment therefore happens exactly once, on whichever
        attempt first finds the row PENDING. See types/enums.py
        AttachmentStatus.

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

        # Read for the comment link and the S3 key, not for the status: the row
        # read here may be stale by the time the transaction runs, so the
        # status is fenced by a condition rather than by this read. comment_id
        # is fixed at initiate, so it cannot go stale.
        attachment = self.get_primary_item(
            PK=self.issue_pk(space_id, issue_id),
            SK=self.attachment_sk(attachment_id))

        head = self.head_attachment_object(attachment)
        size = head["ContentLength"]
        # ContentType is always on a real HeadObject response, since S3 stores
        # binary/octet-stream for an upload that declares nothing. The fallback
        # to the declared type is only so a client that omits it cannot become a
        # KeyError halfway through building the transaction.
        content_type = head.get("ContentType") or attachment.content_type

        # Passed in rather than left to _build_update's own isotime() call, so
        # the value written is the one this method can report back.
        updated_at = isotime()

        # Ordering is load-bearing: CancellationReasons come back positionally.
        # Items 1 and 2 are hand-built ADD-only updates rather than
        # _build_update ones: bumping the Issue's or the comment's version here
        # would make attachment traffic fail a concurrent version-fenced
        # update_issue or update_issue_comment with a spurious
        # DDBVersionConflictError. Item 0 is a real edit to the attachment
        # itself, so bumping its version is correct.
        items = [
            {"Update": self._build_update(
                PK=self.issue_pk(space_id, issue_id),
                SK=self.attachment_sk(attachment_id),
                expected_vals={"status": AttachmentStatus.PENDING},
                # The TTL attribute is removed rather than pushed out: a
                # confirmed attachment must not be reapable at all.
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
                # The Issue was deleted between initiate and now; its
                # partition, this row included, is waiting for the sweep.
                raise DDBMissingError(
                    f"Issue not found: {space_id}#{issue_id}") from exc

            failed, _ = self.failed_reason_item(exc, 2)
            if failed:
                # Likewise for the comment, whose own sweep will remove this
                # row; see handle_issue_comment_deleted.
                raise DDBMissingError(
                    "IssueComment not found: "
                    f"{space_id}#{issue_id}#{attachment.comment_id}") from exc

            self.log_client_error(exc)
            raise DDBInternalError(
                f"Error confirming issue attachment: {exc!s}") from exc

        # The row as written, assembled from what was just written rather than
        # read back: a get_item here would be eventually consistent and could
        # hand back the pre-write row, and a consistent read would be a second
        # charge for values this method already knows. Nothing else writes a
        # PENDING attachment, so the version this write produced is the one it
        # read plus the bump _build_update applies.
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

        A PENDING attachment comes back as a row with no URL rather than as an
        error. There is nothing to download, but a caller looking at a stuck
        upload needs to see it to decide what to do with it: re-sign and finish
        it, or delete it. Refusing the read would leave a row a caller can
        neither see nor act on.

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

        Sorted by sort key, which is by attachment_id, which is by creation
        timestamp: see types/issue.py. Covers the Issue's linked and unlinked
        attachments alike, since both live in this partition; GSI1 is what
        narrows to one comment's.

        The SK prefix is what keeps the Issue's info, comment and blocker rows
        out of the result. That is a key condition rather than a filter, so
        Limit counts only attachment rows.

        PENDING attachments are included. Filtering them out would make a page
        of `limit` items mean nothing and would hide exactly the rows a caller
        needs to see to clean up an upload that never finished.

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

        Read from GSI1, which is the comment link itself. The index is sparse:
        an attachment with no comment_id has no GSI1 keys at all, so it is not
        in the index and cannot appear here, rather than being filtered out of
        a page. See types/issue.py IssueAttachment and BaseObject.serialize for
        how an absent key attr, not a NULL one, is what makes that so.

        Eventually consistent, as every GSI read is, so an attachment linked
        moments ago may not be here yet. That is fine for a read a caller
        drives, and is exactly why neither delete sweep uses this index.

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
        """Delete an IssueAttachment and uncount it if it was counted.

        The object follows the row: this write is what the stream turns into an
        IssueAttachmentDeleted, and handle_issue_attachment_deleted deletes the
        bytes. Nothing here touches S3.

        Only an UPLOADED attachment decrements, because only an UPLOADED one
        was ever counted; see confirm_issue_attachment_uploaded. The status
        comes from a read, so it may already be stale by the time the write
        runs, which is why the decrementing form is attempted rather than
        relied on: exactly delete_blocker_for_sweep's shape, and for the same
        reason. The row must go either way, and reading the condition failure
        as "someone else already did this" is what would leave the row behind.

        A failure to decrement is also the correct outcome when the Issue or
        the linked comment has itself been deleted: the row holding the counter
        is gone, so there is nothing to decrement, and this row is one the
        corresponding sweep would have removed anyway.

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

        The shared half of delete_issue_attachment and the delete sweeps, so a
        row swept out from under a deleted Issue is uncounted by the same rule
        as one a caller deletes.

        with_issue and with_comment are passed through to
        attachment_counter_updates, for a caller that already knows one of the
        parent rows is gone; IssueMixin.handle_issue_deleted runs with the
        Issue's info row already deleted.

        Args:
            attachment (IssueAttachment): the row to delete
            with_issue (bool): decrement the Issue's num_attachments
            with_comment (bool): decrement the comment's, when linked
        """
        if attachment.is_uploaded:
            applied = self.apply_idempotent_transaction(
                [{"Delete": self.uploaded_attachment_delete(attachment)},
                 *self.attachment_counter_updates(
                     attachment, -1, with_issue=with_issue,
                     with_comment=with_comment)],
                "Error deleting issue attachment")

            if applied:
                return

        # Either the attachment was never counted, because it is still PENDING,
        # or the status read above was stale and another writer has already
        # applied the counted delete, or a counter's own row is gone. All three
        # leave the row to delete without a decrement.
        self.delete_row(attachment)

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

    def attachment_counter_updates(self, attachment, delta, *,
                                   with_issue=True, with_comment=True):
        """The counter updates one attachment's status change must carry.

        An UPLOADED attachment is counted on its Issue, and on its IssueComment
        too when it is linked, so the two move together or not at all. Kept in
        one place so confirm and the deletes cannot disagree about which
        counters an attachment touches.

        with_issue and with_comment let a caller leave out a counter whose row
        it already knows is gone: the Issue delete sweep runs with the info row
        deleted, so including it would fail the transaction's condition every
        time and cost the sweep a pointless fall-through.

        Args:
            attachment (IssueAttachment): the attachment being counted
            delta (int): 1 on confirm, -1 on delete
            with_issue (bool): include the Issue's num_attachments
            with_comment (bool): include the comment's, when linked

        Returns:
            list[dict]: transact_write_items entries
        """
        updates = []

        if with_issue:
            updates.append({"Update": self.issue_num_attachments_update(
                attachment.space_id, attachment.issue_id, delta)})

        if with_comment and attachment.comment_id is not None:
            updates.append({"Update": self.comment_num_attachments_update(
                attachment.space_id, attachment.issue_id,
                attachment.comment_id, delta)})

        return updates

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def handle_issue_comment_deleted(self, *, space_id, issue_id, comment_id):
        """Handle an IssueComment having been deleted.

        Triggered by an SQS event; see the IssueMixin docstring for the path.

        A linked IssueAttachment MUST NOT outlive its comment, so the comment's
        attachments go with it.

        The comment's own num_attachments is not moved: that counter is on the
        row that is already gone. The Issue's is, because the Issue is still
        there and the rows being deleted were counted against it, so leaving it
        alone would make it permanently overcount its attachments. Each row
        therefore goes through delete_attachment_with_counters, with the
        comment's counter left out and the decrement conditioned on the row
        still being UPLOADED, which is what keeps this idempotent: a replayed or
        late event finds the row gone, the condition fails, and the plain
        delete_row it falls back to is a no-op.

        Reads the Issue's partition rather than GSI1, deliberately, and for the
        same reason handle_issue_deleted's Phase 2 comment gives: the index is
        eventually consistent, an attachment linked moments before the delete
        may not be in it yet, and nothing retries this sweep, so a row missing
        from an eventually-consistent page is a row that outlives its comment
        for good. The partition read is consistent and the set is closed by the
        time it runs, since an attachment cannot be linked to a comment that is
        gone.

        Needs no "is a live comment standing here" guard, unlike
        handle_issue_deleted. That guard exists because an issue_id is 6
        characters drawn from 62 and a new Issue can take a deleted one's id
        within seconds, making its rows indistinguishable from the dead Issue's.
        A comment_id is a UUIDv7, so a new comment effectively never takes a
        deleted one's id, and an attachment found linked to this comment_id can
        only be one of the deleted comment's own.

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
            # Only the attachments, and only this comment's. Everything else in
            # the partition belongs to the Issue, which is still there: the
            # comment was deleted, not the Issue. Matched positively rather
            # than by excluding known types, so a row type added later is
            # skipped rather than swept.
            if (isinstance(item, IssueAttachment)
                    and item.comment_id == comment_id):
                self.delete_attachment_with_counters(item, with_comment=False)

    def handle_issue_attachment_deleted(self, *, space_id, issue_id,
                                        attachment_id):
        """Handle an IssueAttachment row having been deleted.

        Triggered by an SQS event; see the IssueMixin docstring for the path.

        Deletes the S3 object, so the bytes never outlive the row that named
        them. Every path that removes a row reaches this one: a caller's
        delete, either delete sweep, and DynamoDB's TTL reaping a PENDING
        upload, which is why the object is deleted from here rather than
        alongside each of those writes.

        Naturally idempotent, with no condition to arrange: S3's DeleteObject
        on an unversioned bucket answers 204 whether the key was there or not,
        so a replayed event, and a PENDING row whose upload never happened, are
        both a no-op.

        The key is recomputed from the ids rather than carried in the event.
        The event names the attachment, and where its bytes live is this
        library's business; an event carrying the key would let a stale or
        hand-made event name any object in the bucket for deletion.

        Args:
            space_id (str): id of the issue's space
            issue_id (str): id of the issue
            attachment_id (str): id of the deleted attachment

        Raises:
            DDBArgsError: if space_id is invalid
            StorageInternalError: if this manager has no storage configured, or
                the delete fails
        """
        validate_space_id(space_id)
        self.require_storage()

        key = self.attachment_s3_key(space_id, issue_id, attachment_id)

        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=key)
        except ClientError as exc:
            # Nothing is swallowed here: a delete that failed leaves bytes
            # behind, and the event must be retried or land in the DLQ.
            self.log_client_error(exc)
            raise StorageInternalError(
                f"Error deleting attachment object: {exc!s}") from exc
