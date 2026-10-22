#!/usr/bin/env python3
# scripts/campaign.py
"""Run the funded-founder campaign (campaign spec §5-§7; plan
docs/superpowers/plans/2026-10-21-funded-founder-campaign.md, Task 13). Local only.

  python scripts/campaign.py run start [--lookback-days N]
  python scripts/campaign.py run show [--json]
  python scripts/campaign.py company add --domain D --name N --round R --announced-on YYYY-MM-DD
                                         --source-url U --product "..." --excerpt-file PATH
                                         [--amount A] [--country US]
  python scripts/campaign.py company score <id> --score S --notes "..."
  python scripts/campaign.py company promote <id>
  python scripts/campaign.py company list [--status S] [--json]
  python scripts/campaign.py contact add --company <id> --name "First Last" --title T
                                         [--linkedin URL] [--email E]
  python scripts/campaign.py contact verify <id>... | --all
  python scripts/campaign.py contact draft <id> --opener "..." [--subject "..."] | --generic
  python scripts/campaign.py review | send | resume | followups | weekly
  python scripts/campaign.py bounced|booked <contact-id|email>
  python scripts/campaign.py optout <contact-id|email> [--note TEXT]
  python scripts/campaign.py report [--since D] [--until D]

Env: CAMPAIGN_TARGETS (the targets TOML), CAMPAIGN_POSTAL_ADDRESS (every email's footer),
CAMPAIGN_FROM_ADDRESS (default: the first OUTREACH_FROM_ADDRESSES entry), INBOX_API_URL,
APOLLO_API_KEY, HUNTER_API_KEY. `contact draft` exits 2 when the fact check rejects the
opener (or subject), so the skill can retry once with a new one and then fall back to
--generic; it exits 3 when the fact check could not run at all (stop and tell Ben).
`review` serves the approval page and sends the ticked rows on submit; `weekly` is
`followups` then `review`. The page HTML is written under .campaign/ (gitignored).
"""

import argparse
import contextlib
import json
import logging
import os
import re
import secrets
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from dotenv import load_dotenv

load_dotenv()

import clients.otel as otel
from clients import apollo, gcp_auth, hunter, inbox_api
from clients.claude_cli import ClaudeCliError
from models.campaign import COMPANY_STATUSES, ROUNDS
from repo import campaign as campaign_repo
from services import (
    campaign,
    campaign_draft,
    campaign_factcheck,
    campaign_followups,
    campaign_report,
    campaign_review,
    campaign_send,
    campaign_targets,
    campaign_verify,
)
from services.outreach_report import table

log = logging.getLogger(__name__)

CAMPAIGN_DIR = Path(__file__).resolve().parent.parent / ".campaign"
FACT_CHECK_FAILED = "fact check failed"
REGENERATED = "fact check regenerated"
_DRAFTABLE = ("candidate", "unverified", "drafted")
_VERIFIABLE = ("candidate", "unverified")


class CliError(Exception):
    pass


class FactCheckFailed(Exception):
    """The opener is not supported by the excerpt: exit 2, the skill retries."""

    def __init__(self, claims: list[str]) -> None:
        self.claims = claims
        super().__init__("; ".join(claims))


class FactCheckUnavailable(Exception):
    """The fact check could not run (the claude CLI failed or said nothing): exit 3. Not
    a verdict on the opener, so it is not recorded and --generic is not the answer."""


USER_ERRORS = (
    CliError,
    campaign.InvalidTransition,
    campaign.NotFound,
    campaign_targets.TargetsError,
    campaign_send.PreflightError,
    inbox_api.InboxApiUnavailable,
    apollo.ApolloUnavailable,
    hunter.HunterUnavailable,
    httpx.HTTPError,
    ValueError,
)


# --- configuration --------------------------------------------------------------------------


def _targets() -> campaign_targets.CampaignTargets:
    path = os.environ.get("CAMPAIGN_TARGETS", "").strip()
    if not path:
        raise CliError("CAMPAIGN_TARGETS is unset (scripts/fetch-env.sh writes it)")
    return campaign_targets.load_targets(Path(path))


def _from_address() -> str:
    address = campaign.campaign_from_address()
    if not address or "@" not in address:
        raise CliError("no sender: set CAMPAIGN_FROM_ADDRESS or OUTREACH_FROM_ADDRESSES")
    return address


def _postal_address() -> str:
    return os.environ.get("CAMPAIGN_POSTAL_ADDRESS", "").strip()


def _require_postal_address() -> str:
    postal = _postal_address()
    if not postal:
        raise CliError("CAMPAIGN_POSTAL_ADDRESS is empty; every email carries it")
    return postal


def _run_id(conn: Any) -> int:
    run = campaign_repo.latest_run(conn)
    if run is None:
        raise CliError("no campaign run yet; run `campaign run start` first")
    return run["id"]


def inbox_reachable() -> bool:
    """Any HTTP response from inbox-api's base URL (a 401/403/404 included) proves it is
    up; an unset URL, no token, a connection error or a timeout is unreachable."""
    try:
        base = inbox_api._base()
        token = gcp_auth.id_token_for(base)
        httpx.get(base, headers={"Authorization": f"Bearer {token}"}, timeout=inbox_api.TIMEOUT_S)
    except Exception as e:  # a token minter failure (gcloud missing or erroring) included
        log.warning("inbox-api unreachable: %s: %s", type(e).__name__, e)
        return False
    return True


def _amount(value: str) -> int:
    """`12000000`, `12,000,000`, `$12M` or `500k` → whole USD."""
    m = re.fullmatch(r"\$?([\d,]+(?:\.\d+)?)([kmb]?)", value.strip().lower())
    if not m:
        raise ValueError(f"amount {value!r}: use 12000000, 12M or 500k")
    scale = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[m.group(2)]
    return round(float(m.group(1).replace(",", "")) * scale)


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, default=str)


# --- runs -----------------------------------------------------------------------------------


def cmd_run_start(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    run = campaign.start_run(conn, now=now, targets=_targets(), lookback_days=args.lookback_days)
    conn.commit()
    print(f"run {run['id']}: {run['lookback_from']} to {run['lookback_to']}", file=out)


def cmd_run_show(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    run = campaign_repo.latest_run(conn)
    if run is None:
        raise CliError("no campaign run yet")
    if args.json:
        print(_json(run), file=out)
        return
    print(f"run {run['id']}: {run['lookback_from']} to {run['lookback_to']}", file=out)
    print(f"ran at {run['ran_at']}", file=out)
    counts = {k: v for k, v in (run.get("counts") or {}).items() if v}
    print("counts: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "none"), file=out)
    if run.get("notes"):
        print(f"notes: {run['notes']}", file=out)


# --- companies ------------------------------------------------------------------------------


def cmd_company_add(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    fields = {
        "domain": args.domain,
        "name": args.name,
        "round": args.round,
        "announced_on": date.fromisoformat(args.announced_on),
        "source_url": args.source_url,
        "product_summary": args.product,
        "announcement_excerpt": Path(args.excerpt_file).read_text().strip(),
        "hq_country": args.country,
    }
    if args.amount:
        fields["amount"] = _amount(args.amount)
    row, created = campaign.add_company(conn, fields, run_id=_run_id(conn))
    conn.commit()
    print(f"{'added' if created else 'exists'} {row['id']}", file=out)


def cmd_company_score(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    row = campaign.score_company(
        conn, args.id, score=args.score, notes=args.notes, threshold=_targets().threshold
    )
    conn.commit()
    print(f"{row['status']} {row['id']}", file=out)


def cmd_company_promote(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    row = campaign.promote_company(conn, args.id)
    conn.commit()
    print(f"{row['status']} {row['id']}", file=out)


def cmd_company_list(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    rows = campaign_repo.companies(conn, status=args.status)
    if args.json:
        print(_json(rows), file=out)
        return
    if not rows:
        print("no companies", file=out)
        return
    headers = ["id", "status", "fit", "round", "name", "domain"]
    cells = [
        [
            r["id"],
            r["status"],
            r.get("fit_score") or "",
            r.get("round") or "",
            r["name"],
            r["domain"],
        ]
        for r in rows
    ]
    print(table(headers, cells), file=out)


# --- contacts -------------------------------------------------------------------------------


def _contact(conn: Any, ident: str) -> dict:
    """A contact by id or exact email."""
    if ident.isdigit():
        row = campaign_repo.get_contact(conn, int(ident))
    elif "@" in ident:
        row = campaign_repo.get_contact_by_email(conn, ident)
    else:
        raise CliError(f"{ident}: give a contact id or an email")
    if row is None:
        raise CliError(f"no campaign contact matches {ident}")
    return row


def _company(conn: Any, company_id: int) -> dict:
    row = campaign_repo.get_company(conn, company_id)
    if row is None:
        raise campaign.NotFound(f"no campaign company {company_id}")
    return row


def cmd_contact_add(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    fields = {
        "company_id": args.company,
        "full_name": args.name,
        "title": args.title,
        "linkedin_url": args.linkedin,
    }
    if args.email:
        fields.update(email=args.email.strip(), email_source="manual")
    row, outcome = campaign.add_contact(conn, fields, run_id=_run_id(conn))
    conn.commit()
    print(f"{outcome} {row['id']}", file=out)


# Key, plan and quota problems affect every contact: they abort the command.
_VERIFY_ABORT = (401, 403, 429)


def _verify_failed(conn: Any, contact: dict, reason: str, now: datetime) -> str:
    """Park a contact whose lookup failed for its own sake and let the rest continue."""
    log.warning("verify failed for contact %s: %s", contact["id"], reason)
    conn.rollback()
    if contact["status"] == "candidate":
        campaign.advance(
            conn, contact["id"], "unverified", at=now, source="cli", note=f"verify failed: {reason}"
        )
        conn.commit()
    return f"{contact['id']} {contact.get('email') or '-'} error {reason}"


def _verify_one(conn: Any, contact: dict, now: datetime) -> str:
    """Resolve and store the email, then gate the contact on it: a found address that is
    warm or has hard-bounced parks the contact at `skipped_warm`."""
    if contact["status"] not in _VERIFIABLE:
        raise CliError(f"contact {contact['id']} is at {contact['status']}; nothing to verify")
    company = _company(conn, contact["company_id"])
    try:
        email, verification, source = campaign_verify.resolve_email(
            contact, company, apollo=apollo, hunter=hunter
        )
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in _VERIFY_ABORT or not 400 <= code < 500:
            raise
        return _verify_failed(conn, contact, f"HTTP {code} from {e.request.url.host}", now)
    except ValueError as e:
        return _verify_failed(conn, contact, str(e), now)
    changes: dict[str, Any] = {"verification": verification}
    if email:
        changes = {"email": email, "email_source": source, **changes}
    row = campaign_repo.update_contact(conn, contact["id"], changes)
    # `unverified → candidate` is not a transition: a re-verify that comes back valid leaves
    # the contact at `unverified`, and `contact draft` accepts it (`unverified → drafted`).
    if verification != "valid" and row["status"] == "candidate":
        row = campaign.advance(conn, contact["id"], "unverified", at=now, source="cli").row
    if row.get("email"):
        reason = campaign.sendable(conn, row, company, allowed=_VERIFIABLE)
        if reason in ("warm", "bounced address"):
            campaign.park_warm(conn, contact["id"], reason=reason, at=now, source="cli")
            conn.commit()
            return f"skipped_warm {contact['id']} ({reason})"
    conn.commit()
    return f"{contact['id']} {email or '-'} {verification} {row['status']}"


def cmd_contact_verify(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    if args.all == bool(args.ids):
        raise CliError("give contact ids or --all, not both")
    if args.all:
        contacts = [c for s in _VERIFIABLE for c in campaign_repo.contacts(conn, status=s)]
    else:
        contacts = [_contact(conn, str(i)) for i in args.ids]
    for contact in contacts:
        print(_verify_one(conn, contact, now), file=out)
    if not contacts:
        print("nothing to verify", file=out)


def _fact_check(conn: Any, contact: dict, text: str, excerpt: str, now: datetime) -> str | None:
    """The regeneration note to annotate after drafting (None on a first pass), or
    FactCheckFailed after annotating the failure (committed, so the retry knows it is a
    regeneration). Both are annotations: repo.campaign.counts_by_week skips them.
    `text` is the opener, with a hand-written subject line above it. FactCheckUnavailable
    when the check could not run: nothing is recorded."""
    try:
        result = campaign_factcheck.check(text, excerpt)
    except ClaudeCliError as e:
        raise FactCheckUnavailable(str(e)) from e
    if not result.ok and result.unsupported == ["unparseable"] and not result.raw.strip():
        raise FactCheckUnavailable("the claude CLI returned nothing")
    claims = None if result.ok else result.unsupported
    version = campaign_factcheck.PROMPT_VERSION
    if claims is not None:
        note = f"{FACT_CHECK_FAILED} (prompt {version}): {'; '.join(claims)}"
        campaign.annotate(conn, contact["id"], at=now, source="cli", note=note)
        conn.commit()
        raise FactCheckFailed(claims)
    previous = campaign_repo.latest_event_note(conn, contact["id"]) or ""
    return f"{REGENERATED} (prompt {version})" if previous.startswith(FACT_CHECK_FAILED) else None


def cmd_contact_draft(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    contact = _contact(conn, str(args.id))
    if contact["status"] not in _DRAFTABLE:
        raise CliError(f"contact {contact['id']} is at {contact['status']}; cannot draft")
    if contact.get("verification") != "valid":
        raise CliError(
            f"contact {contact['id']} verification is {contact.get('verification') or 'unset'}; "
            "drafting needs valid (run `contact verify`)"
        )
    company = _company(conn, contact["company_id"])
    reason = campaign.sendable(conn, contact, company, allowed=_DRAFTABLE)
    if reason is not None:
        raise CliError(f"contact {contact['id']} cannot be drafted: {reason}")
    targets, postal = _targets(), _require_postal_address()
    note = None
    if args.generic:
        opener = campaign_draft.GENERIC_OPENER
    elif not args.opener:
        raise CliError("give --opener, or --generic")
    elif not company.get("announcement_excerpt"):
        raise CliError(f"company {company['id']} has no announcement excerpt; use --generic")
    else:
        opener = args.opener
        # The default subject names only the company; a hand-written one is checked too.
        text = f"{args.subject}\n{opener}" if args.subject else opener
        note = _fact_check(conn, contact, text, company["announcement_excerpt"], now)
    first_name = (contact["full_name"].split() or [""])[0]
    subject, body = campaign_draft.render_email(
        first_name=first_name,
        opening_sentence=opener,
        company_name=company["name"],
        subject=args.subject,
        targets=targets,
        postal_address=postal,
    )
    changes = {"subject": subject, "body": body, "opener_generic": args.generic}
    res = campaign.advance(conn, contact["id"], "drafted", at=now, source="cli", changes=changes)
    if not res.changed:  # a re-draft of a drafted row: advance is a no-op, so write the text
        campaign_repo.update_contact(conn, contact["id"], changes)
    if note:  # after the move, so it is the latest note the review page reads
        campaign.annotate(conn, contact["id"], at=now, source="cli", note=note)
    conn.commit()
    print(f"drafted {contact['id']}: {subject}", file=out)


# --- review and send ------------------------------------------------------------------------


def _entry(contact: dict, company: dict, **extra: Any) -> dict:
    return {"contact": contact, "company": company, **extra}


def review_entries(conn: Any, threshold: int) -> tuple[list[dict], dict[str, list[dict]]]:
    """Every drafted contact with its company and flags, and the latest run's parked rows."""
    drafted = []
    for contact in campaign_repo.contacts(conn, status="drafted"):
        note = campaign_repo.latest_event_note(conn, contact["id"]) or ""
        flags = [REGENERATED] if "regenerated" in note else []
        drafted.append(_entry(contact, _company(conn, contact["company_id"]), flags=flags))
    run = campaign_repo.latest_run(conn)
    run_id = run["id"] if run else None
    unverified = [
        _entry(c, _company(conn, c["company_id"]), reason=f"email {c.get('verification')}")
        for c in campaign_repo.contacts(conn, status="unverified", run_id=run_id)
    ]
    warm = [
        _entry(c, _company(conn, c["company_id"]), reason="two-way history or in the referrals")
        for c in campaign_repo.contacts(conn, status="skipped_warm", run_id=run_id)
    ]
    below = []
    for co in campaign_repo.companies(conn, status="below_bar", run_id=run_id):
        score = co.get("fit_score")
        reason = f"fit {score} < {threshold}" if score is not None else co.get("fit_notes") or ""
        below.append(_entry({}, co, reason=reason))
    return drafted, {"unverified": unverified, "skipped_warm": warm, "below_bar": below}


def apply_approvals(conn: Any, approvals: list, now: datetime) -> None:
    for contact_id, status, edited in approvals:
        changes = {"body": edited} if edited and status == "approved" else None
        campaign.advance(conn, contact_id, status, at=now, source="page", changes=changes)
    conn.commit()


def send(conn: Any, out: TextIO, now: datetime, *, force: bool) -> None:
    targets, from_address, postal = _targets(), _from_address(), _postal_address()
    restaged = campaign_send.restage_stale(conn, now=now, stale_after_days=targets.stale_after_days)
    if restaged:
        print(f"restaged {restaged} stale approval(s) to drafted", file=out)
    campaign_send.preflight(
        from_address.split("@")[1], postal_address=postal, inbox_reachable=inbox_reachable
    )
    res = campaign_send.send_batch(
        conn,
        now=now,
        targets=targets,
        from_address=from_address,
        postal_address=postal,
        force=force,
    )
    print(f"sent {res.sent}, remaining {res.remaining}", file=out)
    if res.halted:
        print("halted: more than two bounces; run campaign resume", file=out)
    if res.skipped:
        stopped = ", ".join(f"{cid} ({reason})" for cid, reason in res.skipped)
        print(f"not sent: {stopped}", file=out)
    if res.needs_check:
        ids = ", ".join(str(i) for i in res.needs_check)
        print(
            f"needs check: contacts {ids}; check Outlook Sent Items/Drafts for these "
            "before re-running send today",
            file=out,
        )


def cmd_review(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    targets = _targets()
    drafted, parked = review_entries(conn, targets.threshold)
    if not drafted:
        print("nothing drafted to review", file=out)
        return
    key = secrets.token_hex(16)
    html = campaign_review.render_page(drafted=drafted, parked=parked, key=key)
    run = campaign_repo.latest_run(conn)
    CAMPAIGN_DIR.mkdir(exist_ok=True)
    (CAMPAIGN_DIR / f"review-{run['id'] if run else 'none'}.html").write_text(html)

    def on_submit(approvals: list) -> None:  # spec §5.6: one button approves and sends
        apply_approvals(conn, approvals, now)
        send(conn, out, now, force=False)

    ids = [e["contact"]["id"] for e in drafted]
    campaign_review.serve(html=html, key=key, drafted_ids=ids, on_submit=on_submit)


def cmd_send(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    send(conn, out, now, force=False)


def cmd_resume(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    send(conn, out, now, force=True)  # ignores the bounce halt for this batch


def cmd_followups(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    """Follow-ups are sends: the same preflight, and the same daily cap as new sends."""
    targets, from_address, postal = _targets(), _from_address(), _require_postal_address()
    campaign_send.preflight(
        from_address.split("@")[1], postal_address=postal, inbox_reachable=inbox_reachable
    )
    today = now.astimezone(campaign.TZ).date()
    budget = max(0, targets.per_day - campaign_repo.sent_today(conn, today))
    res = campaign_followups.run(
        conn, now=now, from_address=from_address, postal_address=postal, budget=budget
    )
    line = f"sent {res.sent} (fallback {res.fallback}, skipped {res.skipped}, failed {res.failed})"
    if res.remaining:
        line += f"; remaining {res.remaining} (daily cap reached; run followups tomorrow)"
    if res.failed:
        line += "; see: campaign report (Needs attention)"
    print(line, file=out)


def cmd_weekly(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    cmd_followups(conn, args, out, now)
    cmd_review(conn, args, out, now)


# --- hand moves and report ------------------------------------------------------------------


def cmd_hand(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    contact = _contact(conn, args.contact)
    res = campaign.advance(conn, contact["id"], args.command, at=now, source="cli")
    if args.command == "bounced" and contact.get("email"):
        # The address is dead whatever the contact: stamp it so no later contact gets it.
        campaign_repo.set_people_bounced(conn, contact["email"], now)
    conn.commit()
    print(f"{args.command} {contact['id']}" + ("" if res.changed else " (unchanged)"), file=out)


def cmd_optout(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    contact = _contact(conn, args.contact)
    company_id = contact["company_id"]
    n = campaign.opt_out_company(conn, company_id, at=now, source="cli", note=args.note)
    conn.commit()
    print(f"opted_out company {company_id} ({n} contact(s) stopped)", file=out)


def cmd_report(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    since = date.fromisoformat(args.since) if args.since else None
    until = date.fromisoformat(args.until) if args.until else None
    r = campaign_report.report(conn, since=since, until=until)
    print(campaign_report.format_report(r), file=out)


COMMANDS: dict[str, Callable[[Any, argparse.Namespace, TextIO, datetime], None]] = {
    "run start": cmd_run_start,
    "run show": cmd_run_show,
    "company add": cmd_company_add,
    "company score": cmd_company_score,
    "company promote": cmd_company_promote,
    "company list": cmd_company_list,
    "contact add": cmd_contact_add,
    "contact verify": cmd_contact_verify,
    "contact draft": cmd_contact_draft,
    "review": cmd_review,
    "send": cmd_send,
    "resume": cmd_resume,
    "followups": cmd_followups,
    "weekly": cmd_weekly,
    "bounced": cmd_hand,
    "booked": cmd_hand,
    "optout": cmd_optout,
    "report": cmd_report,
}


def _command(args: argparse.Namespace) -> str:
    action = getattr(args, "action", None)
    return f"{args.command} {action}" if action else args.command


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="campaign", description="Run the funded-founder campaign.")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run").add_subparsers(dest="action", required=True)
    run.add_parser("start").add_argument("--lookback-days", type=int)
    run.add_parser("show").add_argument("--json", action="store_true")

    company = sub.add_parser("company").add_subparsers(dest="action", required=True)
    ca = company.add_parser("add")
    ca.add_argument("--domain", required=True)
    ca.add_argument("--name", required=True)
    ca.add_argument("--round", required=True, choices=ROUNDS)
    ca.add_argument("--announced-on", required=True)
    ca.add_argument("--source-url", required=True)
    ca.add_argument("--product", required=True)
    ca.add_argument("--excerpt-file", required=True)
    ca.add_argument("--amount")
    ca.add_argument("--country", default="US")
    cs = company.add_parser("score")
    cs.add_argument("id", type=int)
    cs.add_argument("--score", type=int, required=True)
    cs.add_argument("--notes", required=True)
    company.add_parser("promote").add_argument("id", type=int)
    cl = company.add_parser("list")
    cl.add_argument("--status", choices=COMPANY_STATUSES)
    cl.add_argument("--json", action="store_true")

    contact = sub.add_parser("contact").add_subparsers(dest="action", required=True)
    co = contact.add_parser("add")
    co.add_argument("--company", type=int, required=True)
    co.add_argument("--name", required=True)
    co.add_argument("--title", required=True)
    co.add_argument("--linkedin")
    co.add_argument("--email")
    cv = contact.add_parser("verify")
    cv.add_argument("ids", nargs="*", type=int)
    cv.add_argument("--all", action="store_true")
    cd = contact.add_parser("draft")
    cd.add_argument("id", type=int)
    cd.add_argument("--opener")
    cd.add_argument("--subject")
    cd.add_argument("--generic", action="store_true")

    for name in ("review", "send", "resume", "followups", "weekly"):
        sub.add_parser(name)
    for name in ("bounced", "booked"):
        sub.add_parser(name).add_argument("contact")
    oo = sub.add_parser("optout")
    oo.add_argument("contact")
    oo.add_argument("--note")
    r = sub.add_parser("report")
    r.add_argument("--since")
    r.add_argument("--until")
    return p


def main_with(
    argv: list[str],
    get_conn: Callable[[], Any],
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    now: datetime | None = None,
) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # argparse has printed usage; 2 is reserved for the fact check
        return 0 if e.code == 0 else 1
    now = now or datetime.now(UTC)
    otel.setup_telemetry("people-campaign-local")
    try:
        with get_conn() as conn:
            try:
                with contextlib.redirect_stderr(err):
                    COMMANDS[_command(args)](conn, args, out, now)
            except FactCheckFailed as e:
                conn.rollback()
                print("fact check failed; unsupported claims:", file=err)
                for claim in e.claims:
                    print(f"  - {claim}", file=err)
                return 2
            except FactCheckUnavailable as e:
                conn.rollback()
                print(f"fact check could not run: {e}", file=err)
                return 3
            except USER_ERRORS as e:
                conn.rollback()
                print(f"error: {e}", file=err)
                return 1
        return 0
    finally:
        # A short-lived local process exports nothing unless it flushes.
        otel.flush()


def main() -> None:
    from clients.db import get_conn

    sys.exit(main_with(sys.argv[1:], get_conn))


if __name__ == "__main__":
    main()
