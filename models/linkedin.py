"""LinkedIn snapshot records (docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §4).

Pure types: built by services/linkedin_export.py, written by repo/linkedin.py.
"""

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class LinkedInConnection:
    profile_url: str  # normalized: linkedin.com/in/<slug>
    first_name: str | None
    last_name: str | None
    full_name: str
    email: str | None
    company: str | None
    position: str | None
    connected_on: date | None
    person_email: str | None = None  # soft link to people.email
    match_method: str | None = None  # 'email' | 'name' | None
    message_count: int = 0
    my_message_count: int = 0
    last_message_at: datetime | None = None
    last_my_message_at: datetime | None = None


@dataclass
class LinkedInMessage:
    conversation_id: str
    conversation_title: str | None
    sender_name: str | None
    sender_profile_url: str | None  # normalized
    recipient_names: str | None
    recipient_profile_urls: list[str]  # normalized
    sent_at: datetime
    subject: str | None
    content: str | None
    folder: str | None
    from_me: bool = False


@dataclass
class LinkedInRecommendation:
    direction: str  # 'given' | 'received'
    first_name: str | None
    last_name: str | None
    full_name: str
    company: str | None
    job_title: str | None
    text: str | None
    status: str | None
    created_on: date | None
    profile_url: str | None = None  # connection with the unique matching name


@dataclass
class LinkedInSnapshot:
    source: str  # export directory or zip basename
    snapshot_at: datetime
    me: str | None  # Ben's normalized profile URL, inferred or --me
    connections: list[LinkedInConnection]
    messages: list[LinkedInMessage]
    recommendations: list[LinkedInRecommendation]
    conversations: int = 0
    messages_by_url: int = 0
    messages_by_name: int = 0
    messages_unmatched: int = 0
    messages_skipped: int = 0  # rows with no conversation id or an unparseable DATE
    missing_files: list[str] = field(default_factory=list)
    matched_by_email: int = 0
    matched_by_name: int = 0
