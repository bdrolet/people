"""iMessage snapshot read endpoints
(docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md §7.3).

Spec §6: no response model here carries message text — stats and link
metadata only. tests/test_api.py enforces this with a guard test."""

from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from clients import db
from repo import imessage

router = APIRouter()


class IMessageSummary(BaseModel):
    handles: list[str]
    message_count: int
    my_message_count: int
    last_message_at: datetime | None
    last_my_message_at: datetime | None
    group_message_count: int
    last_group_message_at: datetime | None
    imported_at: datetime | None


class IMessageHandleOut(BaseModel):
    handle: str
    display_name: str | None
    google_resource_name: str | None
    person_email: str | None
    match_method: str | None
    message_count: int
    my_message_count: int
    last_message_at: datetime | None
    last_my_message_at: datetime | None
    group_message_count: int
    last_group_message_at: datetime | None


class IMessageHandleList(BaseModel):
    results: list[IMessageHandleOut]


class IMessageGroupOut(BaseModel):
    chat_guid: str
    display_name: str | None
    participant_count: int | None
    message_count: int
    last_message_at: datetime | None


class IMessageHandleDetail(IMessageHandleOut):
    groups: list[IMessageGroupOut]


class IMessageImportOut(BaseModel):
    ran_at: datetime
    mode: str
    max_rowid: int
    messages_upserted: int
    messages_deleted: int
    undecoded: int
    handles: int
    matched_by_email: int
    matched_by_google: int
    linked_to_people: int
    chats: int


@router.get("/imessage/handles", response_model=IMessageHandleList)
def list_handles(
    q: str | None = None,
    min_messages: int | None = Query(default=None, ge=0),
    replied: bool | None = None,
    quiet_since: date | None = None,
    unmatched: bool | None = None,
    include_groups: bool = False,
    limit: int = Query(default=50, ge=1, le=500),
) -> IMessageHandleList:
    with db.get_conn() as conn:
        rows = imessage.handles(
            conn,
            q=q,
            min_messages=min_messages,
            replied=replied,
            quiet_since=quiet_since,
            unmatched=unmatched,
            include_groups=include_groups,
            limit=limit,
        )
    return IMessageHandleList(results=[IMessageHandleOut.model_validate(r) for r in rows])


@router.get("/imessage/handles/{handle}", response_model=IMessageHandleDetail)
def get_handle(handle: str) -> IMessageHandleDetail:
    with db.get_conn() as conn:
        row = imessage.handle(conn, handle)
        if row is None:
            raise HTTPException(status_code=404)
        groups = imessage.handle_groups(conn, handle)
    return IMessageHandleDetail(
        **IMessageHandleOut.model_validate(row).model_dump(),
        groups=[IMessageGroupOut.model_validate(g) for g in groups],
    )


@router.get("/imessage/imports/latest", response_model=IMessageImportOut)
def latest_import() -> IMessageImportOut:
    with db.get_conn() as conn:
        row = imessage.latest_import(conn)
    if row is None:
        raise HTTPException(status_code=404)
    return IMessageImportOut.model_validate(row)
