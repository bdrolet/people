"""MCP send result → what happens to the row and the queue (send-queue design, "Failure
handling"). Pure. Written against mcp-server-linkedin 4.30.0's send_message and
reply_to_conversation contracts (`sent`, `retry_safe`, `status`, `thread_id`; a reply adds
`participant`, never read here) and its error codes (`_meta["linkedin.dev/error"].code`);
the sender pins that version so a reshaped result never reaches this unreviewed. 4.30.0's
send_message statuses, result shape and message-text rules are unchanged from 4.29.0;
reply_to_conversation is new, and returns the same statuses.

The thread fallback: 4.30.0's profile probe can't find the top card on some profiles (a
nested section inside it) and returns `recipient_resolution_failed`, retry_safe, nothing
typed. Only that result, and only with an explicit `retry_safe` true and `sent` not true,
lets the sender look the conversation up and reply in it instead (`fallback_eligible`,
`single_thread`).

The one rule above all: a send that may have gone out is never retried automatically. Any
unsent result that is not explicitly retry_safe, and any raised error that can happen
after submission, leaves the row at `check_linkedin` for Ben."""

import re
from dataclasses import dataclass
from urllib.parse import unquote

RATE_LIMIT_MARGIN = 300
RATE_LIMIT_DEFAULT = 3600

# One message's problem: set it aside and move on.
SET_ASIDE_STATUSES = frozenset(
    {"message_unavailable", "composer_occupied", "recipient_resolution_failed", "invalid_message"}
)
# Raised before anything is submitted: the row is safe to put back.
_REQUEUE_ERRORS = {
    "browser_busy": None,
    "auth_required": "login",
    "browser_unavailable": "browser_unavailable",
}
_SET_ASIDE_ERRORS = frozenset({"invalid_argument", "not_found"})


@dataclass(frozen=True)
class Outcome:
    row_status: str  # sent | set_aside | check_linkedin | queued
    reason: str | None
    metric: str  # people.linkedin_sends outcome label
    pause: str | None = None  # None | "until" (self-clearing) | "hold" (Ben resumes)
    pause_seconds: int | None = None
    thread_id: str | None = None
    thread_mismatch: bool = False


def _from_error(code: str, retry_after: int | None) -> Outcome:
    if code in _REQUEUE_ERRORS:
        reason = _REQUEUE_ERRORS[code]
        if reason is None:
            return Outcome("queued", code, "browser_busy")
        return Outcome("queued", reason, "paused", pause="hold")
    if code in _SET_ASIDE_ERRORS:
        return Outcome("set_aside", code, "set_aside")
    if code == "rate_limited":
        wait = (retry_after if retry_after is not None else RATE_LIMIT_DEFAULT) + RATE_LIMIT_MARGIN
        return Outcome("check_linkedin", code, "rate_limited", pause="until", pause_seconds=wait)
    if code == "outcome_unknown":
        return Outcome("check_linkedin", code, "check_linkedin")
    return Outcome("check_linkedin", code, "paused", pause="hold")


def map_result(result: dict, *, row_thread_id: str | None) -> Outcome:
    """`result` is clients/linkedin_mcp.py's normalized shape: the tool's dict, or
    {"error": {"code", "retry_after"}} for an error result."""
    error = result.get("error")
    if isinstance(error, dict):
        return _from_error(str(error.get("code") or "internal"), error.get("retry_after"))
    status = str(result.get("status") or "unknown")
    if result.get("sent") is True:
        thread_id = result.get("thread_id")
        mismatch = bool(
            row_thread_id and thread_id and _thread_key(thread_id) != _thread_key(row_thread_id)
        )
        return Outcome("sent", None, "sent", thread_id=thread_id, thread_mismatch=mismatch)
    if result.get("retry_safe") is not True or status == "outcome_unknown":
        return Outcome("check_linkedin", status, "check_linkedin")
    if status in SET_ASIDE_STATUSES:
        return Outcome("set_aside", status, "set_aside")
    return Outcome("queued", status, "paused", pause="hold")


def _thread_key(thread_id: str) -> str:
    """The MCP reports a reply's thread decoded; a stored id may still be percent-encoded."""
    return unquote(str(thread_id))


def fallback_eligible(result: dict) -> bool:
    """A profile send that typed nothing because the recipient could not be resolved.
    Anything else, above all a result that may have gone out, is never retried."""
    return (
        "error" not in result
        and result.get("status") == "recipient_resolution_failed"
        and result.get("retry_safe") is True
        and result.get("sent") is not True
    )


_THREAD_URL = re.compile(r"/messaging/thread/([^/?#]+)/?(?:[?#]|$)")
# 4.30.0 linkedin/conversations.py `_open_conversation_by_username`: raised only when the
# scan found matching threads, none of them unverified, and fewer than index + 1.
_ONLY_ONE = re.compile(r"index 1 out of range: only 1 thread\(s\) exist")


def single_thread(first: dict, second: dict) -> str | None:
    """The one conversation with this person, or None. `first` and `second` are
    get_conversation(linkedin_username, index=0 / index=1): the first must land on a
    thread, and the second must be refused because exactly one thread exists. A second
    thread, or a scan that could not verify its rows, is ambiguous and gives None."""
    if "error" in first:
        return None
    match = _THREAD_URL.search(str(first.get("url") or ""))
    if match is None:
        return None
    error = second.get("error")
    if not (isinstance(error, dict) and _ONLY_ONE.search(str(error.get("text") or ""))):
        return None
    return unquote(match.group(1))
