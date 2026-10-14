"""The outreach contact doc (contact-docs spec §3): its title, the regenerated Messages
section, the sections file a session writes, and the merge that builds every version of
the doc. Pure, no I/O. Every write goes through `merge`, so the preservation rules (Ben's
notes, the log) can't be skipped. Drive's markdown export escapes punctuation and may
curl apostrophes, so headings and log lines are compared after `_plain`; text this module
doesn't rewrite is kept exactly as exported."""

import re
from dataclasses import dataclass
from datetime import date

from services import outreach
from services.outreach_session import CHANNEL_TITLES

DOC_MESSAGES = 100
FOLDER_ENTRY = "consulting-outreach"
TITLE_PREFIX = "Outreach — "
NEXT_STEPS = ("brief", "nudge 1", "nudge 2", "close", "reply", "intro", "none")
SESSION_SECTIONS = ("who they are", "history", "talking points")
NOTES = "ben's notes"
NOTES_PREAMBLE = (
    "_Ben's own notes: sessions never edit this section. Write here, not in Google "
    "comments: every machine update deletes comments._"
)

_ESCAPED = re.compile(r"\\(.)")
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|~])")
_NEXT_STEP = re.compile(r"\(next step:\s*([^)]*)\)")
_LOG_LINE = re.compile(r"^[-*] \d{4}-\d{2}-\d{2} · \S")


class SectionsError(ValueError):
    """The session's sections file can't be merged; nothing is written."""


@dataclass(frozen=True)
class Sections:
    who: str
    history: str
    next_step: str
    talking_points: str
    log: tuple[str, ...] = ()


def _plain(text: str) -> str:
    """Text as Ben sees it: Drive's export escapes removed, apostrophes straightened."""
    return _ESCAPED.sub(r"\1", text).replace("’", "'")


def _key(heading: str) -> str:
    """`## Talking points (next step: brief)` → `talking points`."""
    text = _plain(heading[3:]).replace("*", "").replace("_", "")
    return text.strip().lower().split(" (", 1)[0].strip()


def _split(markdown: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """The lines before the first `## ` heading, then (heading, body lines) per section."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            sections.append((line.rstrip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return preamble, sections


def _trim(lines: list[str]) -> str:
    return "\n".join(lines).strip("\n").rstrip()


def _escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


def title(person: dict) -> str:
    name = person.get("display_name") or person.get("email") or f"person {person['id']}"
    return TITLE_PREFIX + name


def them(person: dict) -> str:
    return (person.get("display_name") or "").split(" ")[0] or "Them"


def _message_item(m: dict, who: str) -> str:
    text = " ".join((m.get("text") or "").split())
    if text:
        text = _escape(text)
    else:
        text = "[attachment]" if m.get("has_attachments") else "[no text]"
    stamp = m["sent_at"].astimezone(outreach.TZ).strftime("%Y-%m-%d %H:%M")
    return f"- {stamp} {'Ben' if m['from_me'] else _escape(who)}: {text}"


def render_messages(messages: dict[str, list[dict]], who: str, *, today: date) -> str:
    """The `## Messages` section (spec D6): the newest DOC_MESSAGES per channel, oldest
    first, one list item each so Drive keeps them on separate lines. Regenerated on every
    write; never merged."""
    lines = [
        f"## Messages (last {DOC_MESSAGES} per channel, regenerated {today.isoformat()}; "
        "edits here are overwritten)"
    ]
    for channel, channel_title in CHANNEL_TITLES.items():
        recent = (messages.get(channel) or [])[-DOC_MESSAGES:]
        if recent:
            lines += ["", f"### {channel_title}", ""]
            lines += [_message_item(m, who) for m in recent]
    if len(lines) == 1:
        lines += ["", "No direct messages on file."]
    return "\n".join(lines)


def parse_sections(text: str) -> Sections:
    """The file a session writes: Who they are, History, Talking points (next step: …)
    and, optionally, Log lines to append. Anything else is refused."""
    _, sections = _split(text)
    found: dict[str, tuple[str, str]] = {}
    for heading, body in sections:
        key = _key(heading)
        if key not in (*SESSION_SECTIONS, "log"):
            raise SectionsError(
                f"a session writes only Who they are, History, Log and Talking points, "
                f"not {heading!r}"
            )
        if key in found:
            raise SectionsError(f"{heading!r} appears twice")
        found[key] = (heading, _trim(body))
    missing = [k for k in SESSION_SECTIONS if not found.get(k, ("", ""))[1]]
    if missing:
        raise SectionsError(f"missing or empty: {', '.join(missing)}")
    match = _NEXT_STEP.search(_plain(found["talking points"][0]))
    step = match.group(1).strip().lower() if match else ""
    if step not in NEXT_STEPS:
        raise SectionsError(
            f"the Talking points heading needs '(next step: <{' | '.join(NEXT_STEPS)}>)'"
        )
    log = tuple(line.strip() for line in found.get("log", ("", ""))[1].splitlines() if line.strip())
    for line in log:
        if not _LOG_LINE.match(line):
            raise SectionsError(f"a log line is '- YYYY-MM-DD · what happened', not {line!r}")
    return Sections(
        who=found["who they are"][1],
        history=found["history"][1],
        next_step=step,
        talking_points=found["talking points"][1],
        log=log,
    )


def _same_log(line: str) -> str:
    return " ".join(_plain(line).lstrip("-* ").split())


def _kept_preamble(preamble: list[str]) -> str:
    """Text under the header that Ben wrote: every line except the title (the first `# `
    line) and the `people id:` line, with blank lines at either end trimmed. Blank lines
    between paragraphs and any further `# ` heading are kept exactly."""
    lines: list[str] = []
    title_dropped = False
    for line in preamble:
        if not title_dropped and line.startswith("# "):
            title_dropped = True
        elif _plain(line).startswith("people id:"):
            continue
        else:
            lines.append(line)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def merge(
    existing: str | None,
    update: Sections,
    *,
    person: dict,
    stage: str,
    messages: str,
    today: date,
) -> str:
    """The whole new doc. Keeps from `existing`: `Ben's notes` and every section after it
    (Ben's), every log line, unrecognized sections (before Ben's notes) and unrecognized
    text under the header. Rewrites the title, the header line and the session's sections;
    `messages` (render_messages) replaces the Messages section."""
    preamble, sections = _split(existing or "")
    extra = _kept_preamble(preamble)
    log: list[str] = []
    replaced: set[str] = set()
    before_notes: list[str] = []
    notes: list[str] = []  # Ben's notes, then whatever Ben put after them
    for heading, body in sections:
        key = _key(heading)
        block = f"{heading}\n\n{_trim(body)}".rstrip()
        if key == "messages":
            continue
        if key == "log":
            log += [line.rstrip() for line in body if line.strip()]
        elif key in SESSION_SECTIONS and key not in replaced and not notes:
            replaced.add(key)
        elif key == NOTES or notes:
            notes.append(block)
        else:
            before_notes.append(block)

    seen = {_same_log(line) for line in log}
    for line in update.log:
        if _same_log(line) not in seen:
            log.append(line)
            seen.add(_same_log(line))
    if not notes:
        notes = [f"## Ben's notes\n\n{NOTES_PREAMBLE}"]

    parts = [
        f"# {title(person)}",
        f"people id: {person['id']} · stage: {stage} · last updated: {today.isoformat()}",
        *([extra] if extra else []),
        f"## Who they are\n\n{update.who}",
        f"## History\n\n{update.history}",
        "## Log" + ("\n\n" + "\n".join(log) if log else ""),
        f"## Talking points (next step: {update.next_step})\n\n{update.talking_points}",
        *before_notes,
        *notes,
        messages,
    ]
    return "\n\n".join(parts) + "\n"
