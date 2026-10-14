"""WhatsApp snapshot records
(docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §4).

Pure types: built by services/whatsapp_export.py, written by repo/whatsapp.py.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class WhatsAppHandle:
    handle: str  # E.164, or 'lid:<id>' (§6.4)
    jid: str | None = None
    display_name: str | None = None
    person_id: int | None = None
    match_method: str | None = None  # 'phone' | 'lid_phone' | 'name' | None
    # E.164: the handle itself, or a LID's phone from the address book (2026-10-14 §5.3)
    phone: str | None = None


@dataclass
class WhatsAppChat:
    chat_jid: str
    kind: str  # 'direct' | 'group'
    subject: str | None = None
    handle: str | None = None  # 'direct' only
    created_at: datetime | None = None
    member_count: int = 0
    message_count: int = 0
    my_message_count: int = 0
    last_message_at: datetime | None = None


@dataclass
class WhatsAppChatMember:
    chat_jid: str
    handle: str
    person_id: int | None = None
    is_admin: bool = False
    is_active: bool = True


@dataclass
class WhatsAppMessage:
    chat_jid: str
    stanza_id: str
    sender_handle: str | None  # None when from_me
    from_me: bool
    sent_at: datetime | None
    text: str | None
    message_type: int | None
    has_media: bool = False
    media_kind: str | None = None
    source_pk: int = 0


@dataclass
class WhatsAppBatch:
    mode: str  # 'incremental' | 'full'
    watermark: int = 0
    chats: list[WhatsAppChat] = field(default_factory=list)
    handles: list[WhatsAppHandle] = field(default_factory=list)
    members: list[WhatsAppChatMember] = field(default_factory=list)
    messages: list[WhatsAppMessage] = field(default_factory=list)
    # ZPARTNERNAME per handle, populated only from a direct session. The LID name
    # match (§6.5) reads this and nothing else, so a group member's ZCONTACTNAME can
    # never feed it.
    partner_names: dict[str, str] = field(default_factory=dict)
    # 'lid:<id>' -> E.164 via the address book, for handles and members alike
    # (spec 2026-10-14 §5.3). match_handles reads this.
    lid_phones: dict[str, str] = field(default_factory=dict)
    sessions_skipped: int = 0
    duplicate_jids: int = 0
    duplicate_stanza_ids: int = 0
    missing_stanza_ids: int = 0
    senderless_dropped: int = 0
    handles_unnormalized: int = 0
    matched_by_phone: int = 0
    matched_by_name: int = 0
    matched_by_lid_phone: int = 0
    names_partner: int = 0
    names_address_book: int = 0
    names_profile: int = 0
    names_member: int = 0
    names_none: int = 0
