"""LinkedIn snapshot read endpoints
(docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §7.3)."""

from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from clients import db
from repo import linkedin

router = APIRouter()


class LinkedInSummary(BaseModel):
    profile_url: str
    company: str | None
    position: str | None
    connected_on: date | None
    message_count: int
    my_message_count: int
    last_message_at: datetime | None
    last_my_message_at: datetime | None
    snapshot_at: datetime


class LinkedInConnectionOut(LinkedInSummary):
    full_name: str
    email: str | None
    person_email: str | None
    match_method: str | None


class LinkedInConnectionList(BaseModel):
    results: list[LinkedInConnectionOut]


class LinkedInMessageOut(BaseModel):
    sender_name: str | None
    sender_profile_url: str | None
    recipient_names: str | None
    sent_at: datetime
    subject: str | None
    content: str | None
    folder: str | None
    from_me: bool


class LinkedInConversationOut(BaseModel):
    conversation_id: str
    conversation_title: str | None
    messages: list[LinkedInMessageOut]


class LinkedInRecommendationOut(BaseModel):
    direction: str
    full_name: str
    company: str | None
    job_title: str | None
    text: str | None
    status: str | None
    created_on: date | None


class LinkedInConnectionDetail(LinkedInConnectionOut):
    recommendations: list[LinkedInRecommendationOut]
    conversations: list[LinkedInConversationOut]


class LinkedInImportOut(BaseModel):
    snapshot_at: datetime
    source: str
    connections: int
    messages: int
    recommendations_given: int
    recommendations_received: int
    matched_by_email: int
    matched_by_name: int


def group_conversations(rows: list[dict]) -> list[LinkedInConversationOut]:
    """rows arrive newest first; each conversation sits at its newest message."""
    groups: dict[str, LinkedInConversationOut] = {}
    for r in rows:
        group = groups.get(r["conversation_id"])
        if group is None:
            group = groups[r["conversation_id"]] = LinkedInConversationOut(
                conversation_id=r["conversation_id"],
                conversation_title=r.get("conversation_title"),
                messages=[],
            )
        group.messages.append(LinkedInMessageOut.model_validate(r))
    return list(groups.values())


@router.get("/linkedin/connections", response_model=LinkedInConnectionList)
def list_connections(
    q: str | None = None,
    company: str | None = None,
    position: str | None = None,
    min_messages: int | None = Query(default=None, ge=0),
    replied: bool | None = None,
    quiet_since: date | None = None,
    unmatched: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> LinkedInConnectionList:
    with db.get_conn() as conn:
        rows = linkedin.list_connections(
            conn,
            q=q,
            company=company,
            position=position,
            min_messages=min_messages,
            replied=replied,
            quiet_since=quiet_since,
            unmatched=unmatched,
            limit=limit,
        )
    return LinkedInConnectionList(results=[LinkedInConnectionOut.model_validate(r) for r in rows])


@router.get("/linkedin/connections/{slug}", response_model=LinkedInConnectionDetail)
def get_connection(
    slug: str, messages_limit: int = Query(default=100, ge=1, le=1000)
) -> LinkedInConnectionDetail:
    profile_url = f"linkedin.com/in/{slug.strip().strip('/').lower()}"
    with db.get_conn() as conn:
        row = linkedin.get_connection(conn, profile_url)
        if row is None:
            raise HTTPException(status_code=404)
        recommendations = linkedin.recommendations_for(conn, profile_url)
        messages = linkedin.messages_for(conn, profile_url, messages_limit)
    return LinkedInConnectionDetail(
        **LinkedInConnectionOut.model_validate(row).model_dump(),
        recommendations=[LinkedInRecommendationOut.model_validate(r) for r in recommendations],
        conversations=group_conversations(messages),
    )


@router.get("/linkedin/imports/latest", response_model=LinkedInImportOut)
def latest_import() -> LinkedInImportOut:
    with db.get_conn() as conn:
        row = linkedin.latest_import(conn)
    if row is None:
        raise HTTPException(status_code=404)
    return LinkedInImportOut.model_validate(row)
