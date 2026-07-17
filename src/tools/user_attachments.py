"""
User attachment storage — passport scan, bag photo, preference sheet (PDF, e.g.
a bucket list export).

Per CLAUDE.md's Attachments section: binary blobs stored on the user document,
not queryable, not indexed, retrieved whole. Structured, queryable preferences
(carry_on_only, budget, destinations, ...) stay on the document body — see
src/tools/update_user_profile.py. This module only ever handles the binary side.

RavenDB feature used: attachments are a distinct binary channel bound to a
document — PutAttachmentOperation/GetAttachmentOperation — separate from the
document's JSON body, so uploading a 2 MB passport scan never inflates the
tokens spent reading a route or session document. The list of attachments on a
user is read back from RavenDB's own attachment metadata
(session.advanced.get_metadata_for(doc)["@attachments"]) rather than duplicated
as a field on the document.
"""
import io
import logging
from datetime import datetime, timezone
from typing import Optional

from pyravendb.commands.commands_data import PutDocumentCommand
from pyravendb.data.operation import AttachmentType
from pyravendb.raven_operations.operations import GetAttachmentOperation, PutAttachmentOperation

from src.db.client import get_store

log = logging.getLogger(__name__)

ATTACHMENT_TYPES = ("passport_scan", "bag_photo", "preference_sheet")


def _ensure_user_doc(user_id: str) -> None:
    """Attachments can only be attached to a document that already exists."""
    store = get_store()
    with store.open_session() as session:
        existing = session.load(f"users/{user_id}")
    if existing is not None:
        return
    doc = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "@metadata": {"@collection": "Users"},
    }
    store.get_request_executor().execute(PutDocumentCommand(key=f"users/{user_id}", document=doc))


def save_user_attachment(
    user_id: str, attachment_type: str, filename: str, content: bytes, content_type: str
) -> dict:
    if attachment_type not in ATTACHMENT_TYPES:
        raise ValueError(
            f"Unknown attachment_type: {attachment_type!r}, expected one of {ATTACHMENT_TYPES}"
        )

    _ensure_user_doc(user_id)
    store = get_store()
    store.operations.send(
        PutAttachmentOperation(f"users/{user_id}", attachment_type, io.BytesIO(content), content_type)
    )

    log.info(
        "Stored %s attachment for %s (%s, %d bytes)",
        attachment_type, user_id, content_type, len(content),
    )
    return {"saved": True, "attachment_type": attachment_type, "filename": filename, "size": len(content)}


def list_user_attachments(user_id: str) -> list[dict]:
    """Returns [{"Name": ..., "ContentType": ..., "Size": ...}, ...] from RavenDB's
    own attachment metadata — empty list if the user or attachments don't exist."""
    store = get_store()
    with store.open_session() as session:
        doc = session.load(f"users/{user_id}")
        if doc is None:
            return []
        metadata = session.advanced.get_metadata_for(doc)
    return metadata.get("@attachments", [])


def get_user_attachment(user_id: str, attachment_type: str) -> Optional[dict]:
    store = get_store()
    result = store.operations.send(
        GetAttachmentOperation(f"users/{user_id}", attachment_type, AttachmentType.document, None)
    )
    if result is None:
        return None
    return {
        "content": result["response"].content,
        "content_type": result["details"].get("content_type"),
    }
