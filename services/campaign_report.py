"""`campaign report`: weekly funnel table plus the contacts that need a manual look. Pure
apart from the repo calls."""

from dataclasses import dataclass
from datetime import date
from typing import Any

from repo import campaign

# (header, counts_by_week key). `verified` is `drafted`: a contact is drafted only once verified.
COLUMNS = [
    ("found", "companies_found"),
    ("above_bar", "above_bar"),
    ("verified", "drafted"),
    ("approved", "approved"),
    ("sent", "sent"),
    ("followed_up", "followed_up"),
    ("replied", "replied"),
    ("opted_out", "opted_out"),
    ("bounced", "bounced"),
    ("booked", "booked"),
]


@dataclass(frozen=True)
class Report:
    weeks: list[dict]
    attention: list[dict]


def report(conn: Any, *, since: date | None, until: date | None) -> Report:
    return Report(
        weeks=campaign.counts_by_week(conn, since, until),
        attention=campaign.attention_contacts(conn),
    )


def format_table(rows: list[dict]) -> str:
    headers = ["week"] + [h for h, _ in COLUMNS]
    body = [[r["week"].isoformat()] + [str(r[k]) for _, k in COLUMNS] for r in rows]
    totals = {k: sum(r[k] for r in rows) for _, k in COLUMNS}
    body.append(["total"] + [str(totals[k]) for _, k in COLUMNS])
    table = [headers, *body]
    widths = [max(len(line[i]) for line in table) for i in range(len(headers))]

    def fmt(line: list[str]) -> str:
        cells = [line[0].ljust(widths[0])] + [c.rjust(w) for c, w in zip(line[1:], widths[1:])]
        return "  ".join(cells)

    lines = [fmt(headers), *(fmt(b) for b in body[:-1]), fmt(body[-1])]
    sent, replied = totals["sent"], totals["replied"]
    rate = f"{replied / sent * 100:.1f}%" if sent else "--"
    return "\n".join([*lines, f"reply rate {rate}"])


def format_attention(rows: list[dict]) -> str:
    if not rows:
        return "Needs attention: none"
    lines = ["Needs attention:"]
    for r in rows:
        if r["status"] == "approved":
            reason = f"unknown send outcome, draft {r['draft_id']}"
        else:
            reason = r["last_note"]
        lines.append(f"  #{r['id']} {r['email']} ({r['company_name']}) {r['status']} — {reason}")
    return "\n".join(lines)


def format_report(r: Report) -> str:
    return format_table(r.weeks) + "\n\n" + format_attention(r.attention)
