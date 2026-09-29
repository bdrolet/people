"""WhatsApp snapshot read endpoints
(docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §8).

Spec §7: no response model here carries message text — stats and link metadata
only, and no chat's last message text. tests/test_api.py enforces this with a
guard test. §7 also keeps the group roster out: handle detail returns the groups
a handle is in, never who else is in them."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from clients import db
from repo import whatsapp

router = APIRouter()


class WhatsAppSummary(BaseModel):
    """§8.1: aggregated across every handle linked to the person. Non-null when the
    person has group membership but no handle row — for one of those the counters
    are all zero and match_method is null, which is accurate."""

    handles: list[str]
    message_count: int
    my_message_count: int
    last_message_at: datetime | None
    last_my_message_at: datetime | None
    group_message_count: int
    group_count: int
    shared_groups: int
    match_method: str | None
    imported_at: datetime | None


class WhatsAppHandleOut(BaseModel):
    handle: str
    jid: str | None
    display_name: str | None
    person_id: int | None
    person_email: str | None
    match_method: str | None
    message_count: int
    my_message_count: int
    last_message_at: datetime | None
    last_my_message_at: datetime | None
    # Messages this handle SENT in groups — not every message in their groups, which
    # is what imessage_handles.group_message_count means (§4.1).
    group_message_count: int
    last_group_message_at: datetime | None
    group_count: int
    updated_at: datetime | None


class WhatsAppHandleList(BaseModel):
    results: list[WhatsAppHandleOut]


class WhatsAppGroupOut(BaseModel):
    chat_jid: str
    subject: str | None
    member_count: int
    message_count: int
    my_message_count: int
    last_message_at: datetime | None
    is_admin: bool
    is_active: bool


class WhatsAppHandleDetail(WhatsAppHandleOut):
    groups: list[WhatsAppGroupOut]


class WhatsAppChatOut(BaseModel):
    chat_jid: str
    kind: str
    subject: str | None
    handle: str | None
    created_at: datetime | None
    member_count: int
    message_count: int
    my_message_count: int
    last_message_at: datetime | None
    updated_at: datetime | None


class WhatsAppChatList(BaseModel):
    results: list[WhatsAppChatOut]


class WhatsAppImportOut(BaseModel):
    started_at: datetime
    finished_at: datetime
    mode: str
    watermark: int | None
    chats_upserted: int
    members_upserted: int
    handles_upserted: int
    messages_upserted: int
    messages_deleted: int
    senderless_dropped: int
    duplicate_stanza_ids: int
    sessions_skipped: int
    handles_unnormalized: int
    matched_by_phone: int
    matched_by_name: int


@router.get("/whatsapp/handles", response_model=WhatsAppHandleList)
def list_handles(
    q: str | None = None,
    unmatched: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> WhatsAppHandleList:
    with db.get_conn() as conn:
        rows = whatsapp.handles(conn, q=q, unmatched=unmatched, limit=limit)
    return WhatsAppHandleList(results=[WhatsAppHandleOut.model_validate(r) for r in rows])


@router.get("/whatsapp/handles/{handle}", response_model=WhatsAppHandleDetail)
def get_handle(handle: str) -> WhatsAppHandleDetail:
    """`{handle}` needs percent-encoding for a leading `+` (`%2B`), as
    fetching-person documents for phone idents. A `lid:` handle needs none."""
    with db.get_conn() as conn:
        row = whatsapp.handle(conn, handle)
        if row is None:
            raise HTTPException(status_code=404)
        groups = whatsapp.handle_groups(conn, handle)
    return WhatsAppHandleDetail(
        **WhatsAppHandleOut.model_validate(row).model_dump(),
        groups=[WhatsAppGroupOut.model_validate(g) for g in groups],
    )


@router.get("/whatsapp/chats", response_model=WhatsAppChatList)
def list_chats(
    kind: Literal["direct", "group"] | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> WhatsAppChatList:
    with db.get_conn() as conn:
        rows = whatsapp.chats(conn, kind=kind, limit=limit)
    return WhatsAppChatList(results=[WhatsAppChatOut.model_validate(r) for r in rows])


@router.get("/whatsapp/imports/latest", response_model=WhatsAppImportOut)
def latest_import() -> WhatsAppImportOut:
    with db.get_conn() as conn:
        row = whatsapp.latest_import(conn)
    if row is None:
        raise HTTPException(status_code=404)
    return WhatsAppImportOut.model_validate(row)
