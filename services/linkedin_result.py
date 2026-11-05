"""MCP send result → what happens to the row and the queue (send-queue design, "Failure
handling"). Pure. Written against mcp-server-linkedin 4.29.0's send_message contract
(`sent`, `retry_safe`, `status`, `thread_id`) and its error codes
(`_meta["linkedin.dev/error"].code`); the sender pins that version so a reshaped result
never reaches this unreviewed.

The one rule above all: a send that may have gone out is never retried automatically. Any
unsent result that is not explicitly retry_safe, and any raised error that can happen
after submission, leaves the row at `check_linkedin` for Ben."""

from dataclasses import dataclass

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
        mismatch = bool(row_thread_id and thread_id and thread_id != row_thread_id)
        return Outcome("sent", None, "sent", thread_id=thread_id, thread_mismatch=mismatch)
    if result.get("retry_safe") is not True or status == "outcome_unknown":
        return Outcome("check_linkedin", status, "check_linkedin")
    if status in SET_ASIDE_STATUSES:
        return Outcome("set_aside", status, "set_aside")
    return Outcome("queued", status, "paused", pause="hold")
