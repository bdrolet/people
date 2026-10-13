"""What a per-person outreach session boots with (scripts/outreach_sessions.py).
`snapshot` reads everything the DB knows about one pipeline person, including
their recent 1:1 messages on every channel; `context_block` renders it as the
markdown appended to the spawned session's system prompt. Email, web news and
talking points are the session's own job ("Prep a brief" in the
working-outreach-queue skill). Message text is private and never leaves the
machine: it goes only into a local session's prompt."""

from datetime import date, datetime
from typing import Any

from repo import imessage as imessage_repo
from repo import linkedin as linkedin_repo
from repo import outreach as outreach_repo
from repo import people as people_repo
from repo import whatsapp as whatsapp_repo
from services import outreach

NAME_MAX = 60
TEXT_MAX = 300
CHANNEL_TITLES = {"imessage": "iMessage", "whatsapp": "WhatsApp", "linkedin": "LinkedIn"}
PREP_PROMPT = (
    "Prep this outreach brief now: follow the working-outreach-queue skill's \"Prep a "
    'brief" procedure for the person in your system prompt, then wait for Ben.'
)


def _label(person: dict) -> str:
    return person.get("display_name") or person.get("email") or f"person {person['id']}"


def session_name(person: dict) -> str:
    full = f"outreach: {_label(person)}"
    return full if len(full) <= NAME_MAX else full[: NAME_MAX - 1] + "…"


def snapshot(conn: Any, person_id: int, *, limit: int) -> dict | None:
    """None when the person has no pipeline row."""
    row = outreach_repo.pipeline_row(conn, person_id)
    if row is None:
        return None
    person = people_repo.get_by_id(conn, person_id) or {}
    linkedin = linkedin_repo.connection_for_person(conn, person_id)
    linkedin_messages = (
        [
            {
                "sent_at": m["sent_at"],
                "from_me": m["from_me"],
                "text": m["content"],
                "has_attachments": False,
            }
            for m in reversed(linkedin_repo.messages_for(conn, linkedin["profile_url"], limit))
        ]
        if linkedin
        else []
    )
    return {
        "item": outreach.queue_item(row),
        "pipeline": {
            "briefed_at": row["briefed_at"],
            "nudge_count": int(row["nudge_count"] or 0),
        },
        "phone_numbers": list(person.get("phone_numbers") or []),
        "linkedin": linkedin,
        "messages": {
            "imessage": imessage_repo.recent_direct_messages(conn, person_id, limit),
            "whatsapp": whatsapp_repo.recent_direct_messages(conn, person_id, limit),
            "linkedin": linkedin_messages,
        },
    }


def _day(value: datetime | date | None) -> str:
    if value is None:
        return "?"
    if isinstance(value, datetime):
        return value.astimezone(outreach.TZ).date().isoformat()
    return value.isoformat()


def _stage_line(item: dict, pipeline: dict) -> str:
    if item["stage"] == "to_contact":
        return f"to_contact · rank {item['rank']}" if item["rank"] else "to_contact · unranked"
    if item["stage"] == "briefed":
        n = pipeline["nudge_count"]
        due = f" · follow-up due {item['follow_up_on']}" if item["follow_up_on"] else ""
        return f"briefed {_day(pipeline['briefed_at'])} · {n} nudge{'' if n == 1 else 's'}{due}"
    return item["stage"]


def _message_line(m: dict, them: str) -> str:
    text = " ".join((m.get("text") or "").split())
    if not text:
        text = "[attachment]" if m.get("has_attachments") else "[no text]"
    if len(text) > TEXT_MAX:
        text = text[:TEXT_MAX] + "…"
    return f"{_day(m['sent_at'])} {'Ben' if m['from_me'] else them}: {text}"


def context_block(snap: dict, *, today: date) -> str:
    item, person = snap["item"], snap["item"]["person"]
    name = _label(person)
    them = (person.get("display_name") or "").split(" ")[0] or "Them"
    li = snap["linkedin"]
    role = " at ".join(p for p in (person.get("job_title"), person.get("company")) if p)

    lines = [
        f"# Active outreach person: {name}",
        "",
        f"You were opened to prep and work one referral-outreach brief: **{name}**, "
        f"people id `{person['id']}`. Use the `working-outreach-queue` skill; run the CLI "
        "from `~/src/people` (`.venv/bin/python scripts/outreach.py ...`). This context is "
        f"a snapshot from {today.isoformat()}; re-pull anything you need fresh.",
        "",
        "## Identity",
        "",
        f"- people id: {person['id']}",
        f"- Email: {person.get('email') or 'none'}",
        f"- Phones: {', '.join(snap['phone_numbers']) or 'none'}",
    ]
    if role:
        lines.append(f"- Role: {role}")
    if li:
        li_role = " at ".join(p for p in (li.get("position"), li.get("company")) if p)
        lines.append(
            f"- LinkedIn: {li['profile_url']}"
            + (f" · {li_role}" if li_role else "")
            + (f" · connected {_day(li.get('connected_on'))}" if li.get("connected_on") else "")
        )
    else:
        lines.append("- LinkedIn: none linked")
    if item["labels"]:
        lines.append(f"- Labels: {', '.join(item['labels'])}")
    if item["notes"]:
        lines.append(f"- Notes (Google): {' '.join(item['notes'].split())}")
    if item["outreach_notes"]:
        lines.append(f"- Outreach notes: {' '.join(item['outreach_notes'].split())}")

    channels = ", ".join(k for k, v in item["channels"].items() if v) or "none"
    last = ", ".join(f"{k} {v}" for k, v in item["last_messages"].items()) or "none"
    lines += [
        "",
        "## Pipeline",
        "",
        f"- Stage: {_stage_line(item, snap['pipeline'])}",
        f"- Channels: {channels}",
        f"- Last message by channel: {last}",
        "",
        "## Message history (1:1, oldest first)",
    ]
    any_messages = False
    for channel, messages in snap["messages"].items():
        if not messages:
            continue
        any_messages = True
        lines += ["", f"### {CHANNEL_TITLES[channel]}", ""]
        lines += [_message_line(m, them) for m in messages]
    if not any_messages:
        lines += ["", "No direct messages on file."]
    lines += ["", "Work only this person."]
    return "\n".join(lines)
