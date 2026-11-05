"""The LinkedIn send queue (docs/superpowers/specs/2026-11-05-linkedin-send-queue-design.md).
The only writer of linkedin_outbox and linkedin_sender_state. Local scripts call it; no
Cloud Function imports it.

Every function takes an open connection. `run_once` commits itself, because it must
commit the claim before the MCP call (a crash after that leaves a `sending` row, which
the next run turns into `check_linkedin` rather than resending). Everything else leaves
committing to the caller.

Message text is personal data: it is stored and sent, never logged and never put in a
metric label."""

import contextlib
import logging
import random
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any, ContextManager, Protocol

import clients.otel as otel
from models.linkedin_outbox import KINDS, SendConfig
from repo import linkedin_outbox as repo
from services import outreach
from services.linkedin_pacing import (
    caps,
    day_bounds,
    draw_gap,
    in_window,
    next_window_start,
    normalize_message,
    week_bounds,
)
from services.linkedin_result import Outcome, map_result

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=15)
SET_ASIDE_STREAK = 3
MISMATCH_LOOKBACK = timedelta(days=7)
RESOLVE_STATUSES = ("sent", "set_aside", "check_linkedin")


class Sender(Protocol):
    def session_problem(self) -> str | None: ...

    def send(self, username: str, message: str, profile_urn: str | None) -> dict: ...


class OutboxNotFound(LookupError):
    def __init__(self, row_id: int) -> None:
        super().__init__(f"no LinkedIn outbox row {row_id}")


class NotEditable(ValueError):
    pass


class NotResolvable(ValueError):
    pass


class CeilingReached(Exception):
    def __init__(self, c: dict) -> None:
        self.sent_today, self.cap_today = c["sent_today"], c["cap_today"]
        self.sent_week, self.cap_week = c["sent_week"], c["cap_week"]
        super().__init__(
            f"LinkedIn ceiling reached: {self.sent_today}/{self.cap_today} today, "
            f"{self.sent_week}/{self.cap_week} this week"
        )


def _today(now: datetime, cfg: SendConfig) -> date:
    return now.astimezone(cfg.window.tz).date()


def ceiling(conn: Any, *, now: datetime, cfg: SendConfig, state: dict | None = None) -> dict:
    """Today's and this week's counted sends (sent, sending, check_linkedin) against the
    ramp-aware caps, in the window's time zone."""
    state = state if state is not None else repo.get_state(conn)
    cap_today, cap_week = caps(
        cfg, today=_today(now, cfg), ramp_started_on=state["ramp_started_on"]
    )
    sent_today = repo.count_counted_between(conn, *day_bounds(now, cfg.window.tz))
    sent_week = repo.count_counted_between(conn, *week_bounds(now, cfg.window.tz))
    return {
        "sent_today": sent_today,
        "cap_today": cap_today,
        "sent_week": sent_week,
        "cap_week": cap_week,
        "reached": sent_today >= cap_today or sent_week >= cap_week,
    }


def _start_ramp(conn: Any, state: dict, now: datetime, cfg: SendConfig) -> None:
    if state["ramp_started_on"] is None:
        repo.update_state(conn, {"ramp_started_on": _today(now, cfg)})


def _attrs(row: dict, outcome: str) -> dict[str, str]:
    return {"outcome": outcome, "mode": row["mode"], "kind": row["kind"]}


def queue(
    conn: Any,
    *,
    person_id: int | None,
    username: str,
    message: str,
    kind: str,
    send_now: bool,
    now: datetime,
    cfg: SendConfig,
    not_before: datetime | None = None,
    thread_id: str | None = None,
    profile_urn: str | None = None,
    session_hint: str | None = None,
) -> dict:
    """Store an approved message. A send-now row is written as `sending` for the
    approving session to send itself, and refused (nothing written) at the ceiling."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; one of {', '.join(KINDS)}")
    username = (username or "").strip()
    if not username:
        raise ValueError("a LinkedIn username is required")
    text = normalize_message(message)
    state = repo.lock_state(conn)
    if send_now:
        c = ceiling(conn, now=now, cfg=cfg, state=state)
        if c["reached"]:
            otel.linkedin_sends.add(
                1, {"outcome": "refused_ceiling", "mode": "send_now", "kind": kind}
            )
            raise CeilingReached(c)
    _start_ramp(conn, state, now, cfg)
    mode = "send_now" if send_now else "queued"
    row = repo.insert(
        conn,
        person_id=person_id,
        linkedin_username=username,
        profile_urn=profile_urn,
        thread_id=thread_id,
        message=text,
        kind=kind,
        mode=mode,
        status="sending" if send_now else "queued",
        approved_at=now,
        not_before=not_before,
        attempted_at=now if send_now else None,
        session_hint=session_hint,
    )
    otel.linkedin_queued.add(1, {"kind": kind, "mode": mode})
    return row


def apply_outcome(
    conn: Any,
    row: dict,
    outcome: Outcome,
    *,
    now: datetime,
    cfg: SendConfig,
    rng: random.Random,
    at: datetime | None = None,
    manual: bool = False,
) -> dict:
    """Record what happened to a row, and to the queue. `at` is when it was sent (a hand
    send recorded later); `manual` is Ben resolving by hand, which never pauses."""
    at = at or now
    state = repo.lock_state(conn)
    changes: dict[str, Any] = {"status": outcome.row_status, "reason": outcome.reason}
    state_changes: dict[str, Any] = {}
    if outcome.row_status == "sent":
        changes.update(
            sent_at=at,
            sent_thread_id=outcome.thread_id,
            thread_mismatch=outcome.thread_mismatch,
            reason="thread_mismatch" if outcome.thread_mismatch else None,
        )
        last = state["last_sent_at"]
        state_changes.update(
            last_sent_at=max(last, at) if last else at,
            next_gap_seconds=draw_gap(cfg, rng),
            consecutive_set_asides=0,
        )
        if row["person_id"] is not None and row["outreach_event_id"] is None:
            res = outreach.record_send(
                conn, row["person_id"], at=at, channel="linkedin", source="linkedin_outbox"
            )
            if res is not None:
                changes["outreach_event_id"] = res[1]
        otel.linkedin_approval_to_send.record(
            max(0.0, (at - row["approved_at"]).total_seconds()),
            {"mode": row["mode"], "kind": row["kind"]},
        )
    elif outcome.row_status == "set_aside" and not manual:
        streak = int(state["consecutive_set_asides"] or 0) + 1
        state_changes["consecutive_set_asides"] = streak
        if streak >= SET_ASIDE_STREAK and not state["paused_reason"]:
            state_changes["paused_reason"] = "set_aside_streak"
    elif outcome.row_status == "queued":
        changes["attempted_at"] = None
    if outcome.pause == "until" and outcome.pause_seconds is not None:
        state_changes["paused_until"] = now + timedelta(seconds=outcome.pause_seconds)
    elif outcome.pause == "hold" and not state["paused_reason"]:
        state_changes["paused_reason"] = outcome.reason or "unknown"
    if outcome.row_status != "sent" and outcome.reason:
        state_changes["last_error"] = outcome.reason
    updated = repo.update(conn, row["id"], changes)
    if state_changes:
        repo.update_state(conn, state_changes)
    otel.linkedin_sends.add(1, _attrs(row, outcome.metric))
    return updated


def _locked(conn: Any, row_id: int) -> dict:
    row = repo.lock(conn, row_id)
    if row is None:
        raise OutboxNotFound(row_id)
    return row


def apply_result(
    conn: Any, row_id: int, result: dict, *, now: datetime, cfg: SendConfig, rng: random.Random
) -> Outcome:
    """A send-now session reports the MCP's result: the sender's rules, pauses included."""
    row = _locked(conn, row_id)
    if row["status"] != "sending":
        raise NotResolvable(f"row {row_id} is {row['status']}, not sending")
    outcome = map_result(result, row_thread_id=row["thread_id"])
    apply_outcome(conn, row, outcome, now=now, cfg=cfg, rng=rng)
    return outcome


def resolve(
    conn: Any,
    row_id: int,
    status: str,
    *,
    now: datetime,
    cfg: SendConfig,
    rng: random.Random,
    thread_id: str | None = None,
    reason: str | None = None,
) -> dict:
    """Ben (or a session reading the thread) says what happened to a sending or
    check_linkedin row."""
    if status not in RESOLVE_STATUSES:
        raise ValueError(f"status must be one of {', '.join(RESOLVE_STATUSES)}")
    row = _locked(conn, row_id)
    if row["status"] not in ("sending", "check_linkedin"):
        raise NotResolvable(f"row {row_id} is {row['status']}; only sending or check_linkedin")
    mismatch = bool(
        status == "sent" and thread_id and row["thread_id"] and thread_id != row["thread_id"]
    )
    outcome = Outcome(
        status,
        reason or (None if status == "sent" else "by_hand"),
        status,
        thread_id=thread_id,
        thread_mismatch=mismatch,
    )
    return apply_outcome(conn, row, outcome, now=now, cfg=cfg, rng=rng, manual=True)


def record_manual(
    conn: Any,
    *,
    person_id: int | None,
    username: str,
    message: str,
    kind: str,
    at: datetime,
    now: datetime,
    cfg: SendConfig,
    rng: random.Random,
) -> dict:
    """A message Ben sent himself: counts against the ceiling and records the pipeline
    event through the same path as a queued send."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; one of {', '.join(KINDS)}")
    username = (username or "").strip()
    if not username:
        raise ValueError("a LinkedIn username is required")
    text = normalize_message(message)
    state = repo.lock_state(conn)
    _start_ramp(conn, state, now, cfg)
    row = repo.insert(
        conn,
        person_id=person_id,
        linkedin_username=username,
        message=text,
        kind=kind,
        mode="manual",
        status="sending",
        approved_at=at,
        attempted_at=at,
    )
    otel.linkedin_queued.add(1, {"kind": kind, "mode": "manual"})
    return apply_outcome(
        conn, row, Outcome("sent", None, "sent"), now=now, cfg=cfg, rng=rng, at=at, manual=True
    )


def edit(conn: Any, row_id: int, message: str, *, now: datetime) -> dict:
    """New text is a new approval: approved_at resets, so it also goes to the back."""
    row = _locked(conn, row_id)
    if row["status"] != "queued":
        raise NotEditable(f"row {row_id} is {row['status']}; only a queued message is edited")
    return repo.update(conn, row_id, {"message": normalize_message(message), "approved_at": now})


def cancel(conn: Any, row_id: int) -> dict:
    row = _locked(conn, row_id)
    if row["status"] not in ("queued", "set_aside"):
        raise NotEditable(f"row {row_id} is {row['status']}; only queued or set aside is cancelled")
    return repo.update(conn, row_id, {"status": "cancelled"})


def requeue(conn: Any, row_id: int) -> dict:
    """Back in line. For check_linkedin, only once LinkedIn shows it did not go out."""
    row = _locked(conn, row_id)
    if row["status"] not in ("set_aside", "check_linkedin"):
        raise NotEditable(f"row {row_id} is {row['status']}; only set_aside or check_linkedin")
    return repo.update(conn, row_id, {"status": "queued", "reason": None, "attempted_at": None})


def pause(conn: Any, reason: str | None) -> dict:
    repo.lock_state(conn)
    return repo.update_state(conn, {"paused_reason": (reason or "").strip() or "manual"})


def resume(conn: Any) -> dict:
    repo.lock_state(conn)
    return repo.update_state(
        conn, {"paused_until": None, "paused_reason": None, "consecutive_set_asides": 0}
    )


def estimate_send_times(conn: Any, *, now: datetime, cfg: SendConfig) -> dict[int, datetime | None]:
    """When each queued row should go, in claim order: the gap after the last send, the
    minimum gap after that, inside the window, under today's cap. Approximate (real gaps
    are random), and None for every row while the queue is held for Ben."""
    state = repo.get_state(conn)
    rows = [r for r in repo.list_rows(conn, ("queued",))]
    if state["paused_reason"]:
        return {r["id"]: None for r in rows}
    c = ceiling(conn, now=now, cfg=cfg, state=state)
    tz = cfg.window.tz
    t = now
    if state["last_sent_at"] is not None:
        t = max(
            t, state["last_sent_at"] + timedelta(seconds=state["next_gap_seconds"] or cfg.gap_min)
        )
    if state["paused_until"] is not None:
        t = max(t, state["paused_until"])
    day, used = _today(now, cfg), c["sent_today"]
    out: dict[int, datetime | None] = {}
    for r in rows:
        if r["not_before"] is not None:
            t = max(t, r["not_before"])
        t = next_window_start(t, cfg.window)
        if t.astimezone(tz).date() != day:
            day, used = t.astimezone(tz).date(), 0
        cap, _ = caps(cfg, today=day, ramp_started_on=state["ramp_started_on"] or _today(now, cfg))
        if used >= cap:
            t = next_window_start(day_bounds(t, tz)[1], cfg.window)
            day, used = t.astimezone(tz).date(), 0
        out[r["id"]] = t
        used += 1
        t = t + timedelta(seconds=cfg.gap_min)
    return out


def status(conn: Any, *, now: datetime, cfg: SendConfig) -> dict:
    state = repo.get_state(conn)
    c = ceiling(conn, now=now, cfg=cfg, state=state)
    stats = repo.queue_stats(conn, mismatch_since=now - MISMATCH_LOOKBACK)
    est = estimate_send_times(conn, now=now, cfg=cfg)
    first = next(iter(est.values()), None)
    return {
        **c,
        "queued": int(stats["queued"]),
        "oldest_approved_at": stats["oldest_approved_at"],
        "check_linkedin": int(stats["check_linkedin"]),
        "set_aside": int(stats["set_aside"]),
        "thread_mismatch": int(stats["thread_mismatch"]),
        "paused_reason": state["paused_reason"],
        "paused_until": state["paused_until"]
        if state["paused_until"] and state["paused_until"] > now
        else None,
        "last_sent_at": state["last_sent_at"],
        "last_error": state["last_error"],
        "next_send_at": first,
        "in_window": in_window(now, cfg.window),
    }


def today_summary(conn: Any, *, now: datetime, cfg: SendConfig) -> dict:
    """The `outreach today` line: what Ben has to act on."""
    state = repo.get_state(conn)
    stats = repo.queue_stats(conn, mismatch_since=now - MISMATCH_LOOKBACK)
    oldest = stats["oldest_approved_at"]
    paused = state["paused_reason"] or (
        "rate_limited" if state["paused_until"] and state["paused_until"] > now else None
    )
    return {
        "queued": int(stats["queued"]),
        "oldest_age_hours": round((now - oldest).total_seconds() / 3600, 1) if oldest else None,
        "paused": paused,
        "check_linkedin": int(stats["check_linkedin"]),
        "set_aside": int(stats["set_aside"]),
        "thread_mismatch": int(stats["thread_mismatch"]),
    }


def _emit_gauges(conn: Any, now: datetime, cfg: SendConfig) -> None:
    state = repo.get_state(conn)
    c = ceiling(conn, now=now, cfg=cfg, state=state)
    stats = repo.queue_stats(conn, mismatch_since=now - MISMATCH_LOOKBACK)
    oldest = stats["oldest_approved_at"]
    paused = state["paused_reason"] or (
        "rate_limited" if state["paused_until"] and state["paused_until"] > now else None
    )
    otel.set_linkedin_gauges(
        {
            "queue_depth": float(stats["queued"]),
            "oldest_age_seconds": (now - oldest).total_seconds() if oldest else 0.0,
            "sends_today": float(c["sent_today"]),
            "sends_week": float(c["sent_week"]),
            "cap_today": float(c["cap_today"]),
            "cap_week": float(c["cap_week"]),
            "heartbeat": now.timestamp(),
            "paused": 1.0 if paused else 0.0,
        },
        paused,
    )


def _sweep_stale(conn: Any, now: datetime) -> None:
    for row in repo.stale_sending(conn, now - STALE_AFTER):
        repo.update(conn, row["id"], {"status": "check_linkedin", "reason": "stale_sending"})
        otel.linkedin_sends.add(1, _attrs(row, "check_linkedin"))


def _gate(conn: Any, now: datetime, cfg: SendConfig) -> str | None:
    """Why this run sends nothing, or None to go ahead."""
    state = repo.get_state(conn)
    if not in_window(now, cfg.window):
        return "outside_window"
    if state["paused_reason"] or (state["paused_until"] and state["paused_until"] > now):
        return "paused"
    if state["last_sent_at"] is not None:
        gap = state["next_gap_seconds"] or cfg.gap_min
        if now < state["last_sent_at"] + timedelta(seconds=gap):
            return "gap"
    if ceiling(conn, now=now, cfg=cfg, state=state)["reached"]:
        return "ceiling"
    return None


def run_once(
    conn: Any,
    sender_factory: Callable[[], ContextManager[Sender]],
    *,
    now: datetime,
    cfg: SendConfig,
    rng: random.Random,
) -> str:
    """One sender tick: at most one message. Returns a word for the log (never text)."""
    try:
        _sweep_stale(conn, now)
        state = repo.lock_state(conn)
        if state["paused_until"] is not None and state["paused_until"] <= now:
            repo.update_state(conn, {"paused_until": None})
        conn.commit()

        word = _gate(conn, now, cfg)
        if word is not None:
            conn.commit()
            return word
        repo.lock_state(conn)  # serialize against a send-now's ceiling check
        if ceiling(conn, now=now, cfg=cfg)["reached"]:
            conn.commit()
            return "ceiling"
        row = repo.claim_next(conn, now)
        if row is None:
            conn.commit()
            return "empty"
        row = repo.update(conn, row["id"], {"status": "sending", "attempted_at": now})
        conn.commit()  # durable before the MCP is touched

        return _send(conn, row, sender_factory, now=now, cfg=cfg, rng=rng)
    finally:
        with contextlib.suppress(Exception):
            _emit_gauges(conn, now, cfg)
            conn.commit()


def _send(
    conn: Any,
    row: dict,
    sender_factory: Callable[[], ContextManager[Sender]],
    *,
    now: datetime,
    cfg: SendConfig,
    rng: random.Random,
) -> str:
    stack = contextlib.ExitStack()
    try:
        sender = stack.enter_context(sender_factory())
        problem = sender.session_problem()
    except Exception as e:  # nothing was submitted: put it back, try next tick
        stack.close()
        logger.warning("linkedin sender: MCP unavailable (%s)", type(e).__name__)
        repo.update(conn, row["id"], {"status": "queued", "attempted_at": None})
        repo.update_state(conn, {"last_error": "mcp_unavailable"})
        conn.commit()
        return "mcp_unavailable"
    try:
        if problem is not None:
            outcome = Outcome("queued", problem, "paused", pause="hold")
        else:
            try:
                result = sender.send(row["linkedin_username"], row["message"], row["profile_urn"])
            except Exception as e:
                logger.warning("linkedin sender: send raised %s", type(e).__name__)
                result = {"error": {"code": "outcome_unknown", "retry_after": None}}
            outcome = map_result(result, row_thread_id=row["thread_id"])
    finally:
        # Shutting the MCP down must never lose the outcome of a send.
        with contextlib.suppress(Exception):
            stack.close()
    apply_outcome(conn, row, outcome, now=now, cfg=cfg, rng=rng)
    conn.commit()
    return problem if problem is not None else outcome.metric
