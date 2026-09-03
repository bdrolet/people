"""Typed payloads arriving on the email-events topic. Mirrors what inbox
publishes (inbox services/email_events.py). Domain events, not commands."""

from typing import Literal, NotRequired, TypedDict


class EmailClassifiedEvent(TypedDict):
    event: Literal["email_classified"]
    message_id: str
    category: str  # urgent | respond | review | reference | ignore
    importance: str
    confidence: float
    subject: str
    sender: str
    sender_display: str
    to: list[str]
    cc: list[str]
    received_at: str  # ISO-8601
    tags: list[str]
    reasoning: str
    body: str
    body_html: str | None
    web_link: str | None
    graph_message_id: NotRequired[str]
    has_attachments: NotRequired[bool]
    is_meeting_message: NotRequired[bool]


EmailSentEvent = TypedDict(
    "EmailSentEvent",
    {
        "event": Literal["email_sent"],
        "graph_message_id": str,
        "conversation_id": str | None,
        "sent_at": str,  # ISO-8601
        "from": str,
        "to": list[str],
        "cc": list[str],
        "subject": str,
    },
)
