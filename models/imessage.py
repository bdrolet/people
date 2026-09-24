"""iMessage snapshot records (docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md §4).

Pure types: built by services/imessage_export.py, written by repo/imessage.py.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class IMessageHandle:
    handle: str  # E.164 phone or lowercased email
    display_name: str | None = None
    google_resource_name: str | None = None
    person_email: str | None = None
    match_method: str | None = None  # 'email' | 'google' | None


@dataclass
class IMessageChat:
    chat_guid: str
    display_name: str | None
    is_group: bool
    participant_handles: list[str]
    last_message_at: datetime | None = None


@dataclass
class IMessageMessage:
    guid: str
    chat_guid: str
    sender_handle: str | None  # None when from_me
    from_me: bool
    sent_at: datetime
    text: str | None
    service: str | None
    has_attachments: bool = False
    edited_at: datetime | None = None
    retracted: bool = False


@dataclass
class IMessageBatch:
    mode: str  # 'incremental' | 'full'
    max_rowid: int
    chats: list[IMessageChat] = field(default_factory=list)
    messages: list[IMessageMessage] = field(default_factory=list)
    handles: list[IMessageHandle] = field(default_factory=list)
    undecoded: int = 0
    retracted: int = 0
    short_codes_dropped: int = 0
    reactions_skipped: int = 0
    senderless_dropped: int = 0
    matched_by_email: int = 0
    matched_by_google: int = 0
    linked_to_people: int = 0
    all_guids: set[str] | None = None
    all_chat_guids: set[str] | None = None
