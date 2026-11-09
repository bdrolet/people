"""Pure logic behind the iMessage MCP server (mcp_servers/imessage.py): render chat.db rows,
resolve a recipient to a chat, and read a send's outcome back out of chat.db. No I/O —
consumes what clients/imessage_local.py's live reads return."""

from dataclasses import dataclass
from typing import Any

from clients.imessage_local import ChatIndex
from services.imessage_export import apple_ts, decode_attributed_body, is_reaction, normalize_handle

GROUP_STYLE = 43


def message_text(row: dict[str, Any]) -> str | None:
    text = row.get("text")
    if text:
        return str(text)
    return decode_attributed_body(row.get("attributedBody"))


def _iso(value: Any) -> str | None:
    at = apple_ts(value)
    return at.isoformat() if at else None


def render_message(row: dict[str, Any]) -> dict[str, Any] | None:
    """One message for a tool result; None for a tapback (not a message of its own)."""
    if is_reaction(row.get("associated_message_type")):
        return None
    from_me = bool(row.get("is_from_me"))
    out: dict[str, Any] = {
        "id": row["ROWID"],
        "at": _iso(row.get("date")),
        "from": "me" if from_me else (row.get("handle") or "unknown"),
        "text": message_text(row),
        "service": row.get("service"),
    }
    if row.get("cache_has_attachments"):
        out["attachments"] = True
    if from_me:
        out["status"] = outgoing_status(row)
    return out


def outgoing_status(row: dict[str, Any]) -> str:
    if row.get("error"):
        return "failed"
    if row.get("is_read"):
        return "read"
    if row.get("is_delivered"):
        return "delivered"
    return "sent"


def participants(index: ChatIndex, chat_rowid: int) -> list[str]:
    out = []
    for handle_rowid in index.chat_handles.get(chat_rowid, []):
        raw = index.handles.get(handle_rowid)
        if raw:
            out.append(normalize_handle(raw) or raw)
    return out


def render_chat(index: ChatIndex, chat: dict[str, Any]) -> dict[str, Any]:
    who = participants(index, chat["ROWID"])
    return {
        "chat": chat["guid"],
        "name": chat.get("display_name") or ", ".join(who) or None,
        "group": chat.get("style") == GROUP_STYLE,
        "participants": who,
        "service": chat.get("service_name"),
        "last_message_at": _iso(chat.get("last_date")),
    }


def is_chat_guid(ident: str) -> bool:
    return ";" in ident


def find_chats(index: ChatIndex, ident: str) -> list[dict[str, Any]]:
    """The chats `ident` names, newest first: the one chat for a chat guid, else every 1:1 chat
    with that phone number or email (a person can have separate iMessage and SMS threads)."""
    ident = ident.strip()
    if is_chat_guid(ident):
        return [c for c in index.chats if c["guid"] == ident]
    target = normalize_handle(ident)
    if target is None:
        return []
    return [
        c
        for c in index.chats
        if c.get("style") != GROUP_STYLE and participants(index, c["ROWID"]) == [target]
    ]


@dataclass
class Target:
    """Where a send goes: an existing chat (by guid), or a new iMessage conversation."""

    handle: str | None
    chat: dict[str, Any] | None

    def describe(self, index: ChatIndex) -> dict[str, Any]:
        if self.chat is not None:
            return {"kind": "existing chat", **render_chat(index, self.chat)}
        return {"kind": "new iMessage conversation", "participants": [self.handle]}


class RecipientError(ValueError):
    pass


def resolve_target(index: ChatIndex, recipient: str) -> Target:
    """A chat guid sends to that chat. A phone number or email sends to the most recent 1:1
    chat with it (the thread Ben would reply in), or starts a new iMessage conversation."""
    recipient = recipient.strip()
    chats = find_chats(index, recipient)
    if is_chat_guid(recipient):
        if not chats:
            raise RecipientError(f"no chat {recipient} in chat.db")
        return Target(handle=None, chat=chats[0])
    handle = normalize_handle(recipient)
    if handle is None:
        raise RecipientError(
            f"{recipient!r} is not a phone number (E.164, e.g. +15550100001) or an email address"
        )
    return Target(handle=handle, chat=chats[0] if chats else None)


def _same_text(a: str | None, b: str) -> bool:
    return (a or "").strip() == b.strip()


def confirm_send(
    rows: list[dict[str, Any]], text: str, *, chat_rowid: int | None, handle: str | None
) -> dict[str, Any] | None:
    """The outgoing row this send wrote, as {status, id, chat}, or None if it isn't there yet.
    Matches on text, then on the chat (or, for a new conversation, the 1:1 handle)."""
    for row in rows:
        if not row.get("is_from_me") or not _same_text(message_text(row), text):
            continue
        if chat_rowid is not None and row.get("chat_id") != chat_rowid:
            continue
        if chat_rowid is None and handle is not None:
            raw = row.get("handle")
            if raw and normalize_handle(raw) != handle:
                continue
        return {"status": outgoing_status(row), "id": row["ROWID"], "chat_id": row.get("chat_id")}
    return None


def search(rows: list[dict[str, Any]], query: str, *, limit: int) -> list[dict[str, Any]]:
    """Case-insensitive substring search over rendered messages, newest first."""
    needle = query.casefold()
    hits = []
    for row in rows:
        msg = render_message(row)
        if msg is None or not msg["text"] or needle not in msg["text"].casefold():
            continue
        hits.append({**msg, "chat_id": row.get("chat_id")})
        if len(hits) >= limit:
            break
    return hits
