from datetime import date
from unittest.mock import patch

from models.campaign import CONTACT_STATUSES
from services import campaign_report as rpt


def week(d, **kw):
    return {
        "week": d,
        "companies_found": 0,
        "above_bar": 0,
        **dict.fromkeys(CONTACT_STATUSES, 0),
        **kw,
    }


ROWS = [
    week(date(2026, 10, 12), companies_found=10, above_bar=4, drafted=3, approved=3, sent=5),
    week(
        date(2026, 10, 19), companies_found=6, above_bar=2, drafted=2, approved=2, sent=3, replied=1
    ),
]


def test_format_table_has_header_totals_and_reply_rate():
    lines = rpt.format_table(ROWS).splitlines()
    assert lines[0].split() == [
        "week", "found", "above_bar", "verified", "approved", "sent",
        "followed_up", "replied", "opted_out", "bounced", "booked",
    ]  # fmt: skip
    assert lines[1].split()[:6] == ["2026-10-12", "10", "4", "3", "3", "5"]
    assert lines[3].split()[:6] == ["total", "16", "6", "5", "5", "8"]
    assert lines[-1] == "reply rate 12.5%"
    assert len({len(line) for line in lines[:4]}) == 1


def test_zero_sent_prints_dashes():
    out = rpt.format_table([week(date(2026, 10, 12), companies_found=1)])
    assert out.splitlines()[-1] == "reply rate --"


def test_format_attention():
    assert rpt.format_attention([]) == "Needs attention: none"
    rows = [
        {"id": 4, "email": "a@x.io", "company_name": "X", "status": "approved",
         "draft_id": "d9", "last_note": None},
        {"id": 5, "email": "b@y.io", "company_name": "Y", "status": "followed_up",
         "draft_id": None, "last_note": "follow-up: sending"},
    ]  # fmt: skip
    lines = rpt.format_attention(rows).splitlines()
    assert lines[0] == "Needs attention:"
    assert lines[1].strip() == "#4 a@x.io (X) approved — unknown send outcome, draft d9"
    assert lines[2].strip() == "#5 b@y.io (Y) followed_up — follow-up: sending"


def test_report_and_format_report():
    with (
        patch("services.campaign_report.campaign.counts_by_week", return_value=ROWS) as w,
        patch("services.campaign_report.campaign.attention_contacts", return_value=[]),
    ):
        r = rpt.report(object(), since=date(2026, 10, 1), until=None)
    assert w.call_args.args[1:] == (date(2026, 10, 1), None)
    assert r.weeks == ROWS and r.attention == []
    out = rpt.format_report(r)
    assert out == rpt.format_table(ROWS) + "\n\nNeeds attention: none"
