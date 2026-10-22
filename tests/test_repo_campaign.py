from datetime import UTC, date, datetime

import pytest

from models.campaign import CONTACT_STATUSES
from repo import campaign


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.rowcount = len(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple | None]] = []
        self._results = list(results or [])

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return FakeCursor(self._results.pop(0) if self._results else [])


TS = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)


def test_insert_run():
    conn = FakeConn([[{"id": 3}]])
    row = campaign.insert_run(
        conn, ran_at=TS, lookback_from=date(2026, 10, 1), lookback_to=date(2026, 10, 20)
    )
    assert row == {"id": 3}
    sql, params = conn.calls[0]
    assert sql.startswith("INSERT INTO campaign_runs (ran_at, lookback_from, lookback_to)")
    assert "RETURNING" in sql
    assert params == (TS, date(2026, 10, 1), date(2026, 10, 20))


def test_latest_run_orders_newest_first_and_may_be_none():
    conn = FakeConn([[]])
    assert campaign.latest_run(conn) is None
    assert "FROM campaign_runs ORDER BY ran_at DESC, id DESC LIMIT 1" in conn.calls[0][0]


def test_update_run_counts_writes_json():
    conn = FakeConn()
    campaign.update_run_counts(conn, 3, {"sent": 2})
    sql, params = conn.calls[0]
    assert sql == "UPDATE campaign_runs SET counts = %s::jsonb WHERE id = %s"
    assert params == ('{"sent": 2}', 3)


def test_insert_company_if_missing_does_nothing_on_conflict():
    conn = FakeConn([[]])
    assert campaign.insert_company_if_missing(conn, {"domain": "Acme.io", "name": "Acme"}) is None
    sql, params = conn.calls[0]
    assert "INSERT INTO campaign_companies (domain, name) VALUES (%s, %s)" in sql
    assert "ON CONFLICT (lower(domain)) DO NOTHING" in sql
    assert "RETURNING" in sql
    assert params == ("Acme.io", "Acme")


def test_insert_company_returns_the_new_row():
    conn = FakeConn([[{"id": 1, "domain": "acme.io"}]])
    assert campaign.insert_company_if_missing(conn, {"domain": "acme.io", "name": "A"})["id"] == 1


def test_insert_company_rejects_unknown_column():
    with pytest.raises(ValueError):
        campaign.insert_company_if_missing(FakeConn(), {"domain": "a.io", "id": 5})


def test_get_company_by_domain_is_case_insensitive():
    conn = FakeConn([[{"id": 1}]])
    assert campaign.get_company_by_domain(conn, "Acme.IO") == {"id": 1}
    sql, params = conn.calls[0]
    assert "WHERE lower(domain) = lower(%s)" in sql and params == ("Acme.IO",)


def test_get_company():
    conn = FakeConn([[]])
    assert campaign.get_company(conn, 9) is None
    assert "WHERE id = %s" in conn.calls[0][0] and conn.calls[0][1] == (9,)


def test_update_company_only_touches_allowlisted_columns():
    conn = FakeConn([[{"id": 1}]])
    campaign.update_company(conn, 1, {"status": "warm", "fit_score": 4})
    sql, params = conn.calls[0]
    assert "UPDATE campaign_companies SET status = %s, fit_score = %s, updated_at = now()" in sql
    assert "WHERE id = %s RETURNING" in sql
    assert params == ("warm", 4, 1)
    for bad in ({"domain": "x.io"}, {"run_id": 2}, {"notes": "n"}, {}):
        with pytest.raises(ValueError):
            campaign.update_company(conn, 1, bad)


def test_companies_filters():
    conn = FakeConn([[{"id": 1}], [{"id": 2}]])
    assert campaign.companies(conn) == [{"id": 1}]
    assert "WHERE" not in conn.calls[0][0] and conn.calls[0][1] == ()
    campaign.companies(conn, status="active", run_id=4)
    sql, params = conn.calls[1]
    assert "WHERE status = %s AND run_id = %s" in sql and params == ("active", 4)


def test_insert_contact_if_missing():
    conn = FakeConn([[]])
    fields = {"company_id": 1, "full_name": "Ann", "email": "A@acme.io"}
    assert campaign.insert_contact_if_missing(conn, fields) is None
    sql, params = conn.calls[0]
    assert "INSERT INTO campaign_contacts (company_id, full_name, email)" in sql
    assert "ON CONFLICT (company_id, lower(email)) DO NOTHING" in sql
    assert params == (1, "Ann", "A@acme.io")
    with pytest.raises(ValueError):
        campaign.insert_contact_if_missing(conn, {"company_id": 1, "created_at": TS})


def test_contact_lookups():
    conn = FakeConn([[{"id": 1}], [{"id": 1}], [{"id": 1}, {"id": 2}]])
    campaign.get_contact(conn, 1)
    assert "WHERE id = %s" in conn.calls[0][0]
    campaign.get_contact_by_email(conn, "A@x.io")
    assert "WHERE lower(email) = lower(%s)" in conn.calls[1][0]
    assert conn.calls[1][1] == ("A@x.io",)
    assert len(campaign.contacts_by_domain(conn, "x.io")) == 2
    sql, params = conn.calls[2]
    assert "JOIN campaign_companies" in sql and "lower(co.domain) = lower(%s)" in sql
    assert params == ("x.io",)


def test_lock_contact_locks_the_row():
    conn = FakeConn([[{"id": 1, "status": "approved"}]])
    assert campaign.lock_contact(conn, 1)["status"] == "approved"
    sql, params = conn.calls[0]
    assert sql.endswith("FOR UPDATE") and params == (1,)


def test_update_contact_only_touches_allowlisted_columns():
    conn = FakeConn([[{"id": 1}]])
    campaign.update_contact(conn, 1, {"status": "sent", "follow_up_on": None})
    sql, params = conn.calls[0]
    assert "UPDATE campaign_contacts SET status = %s, follow_up_on = %s, updated_at = now()" in sql
    assert params == ("sent", None, 1)
    for bad in ({"id": 2}, {"company_id": 2}, {"created_at": TS}):
        with pytest.raises(ValueError):
            campaign.update_contact(conn, 1, bad)


def test_contacts_filters():
    conn = FakeConn([[], []])
    campaign.contacts(conn)
    assert "WHERE" not in conn.calls[0][0] and conn.calls[0][1] == ()
    campaign.contacts(conn, status="sent", run_id=2, follow_up_due_on=date(2026, 10, 28))
    sql, params = conn.calls[1]
    assert "WHERE status = %s AND run_id = %s AND follow_up_on <= %s" in sql
    assert params == ("sent", 2, date(2026, 10, 28))


def test_insert_event_returns_the_new_id():
    conn = FakeConn([[{"id": 42}]])
    eid = campaign.insert_event(
        conn, contact_id=1, company_id=2, status="sent", at=TS, source="cli", note="n"
    )
    assert eid == 42
    sql, params = conn.calls[0]
    assert "INSERT INTO campaign_events" in sql and "RETURNING id" in sql
    assert params == (1, 2, "sent", TS, "cli", None, "n")


def test_sent_today_casts_at_in_the_given_zone():
    conn = FakeConn([[{"n": 3}]])
    assert campaign.sent_today(conn, date(2026, 10, 21), "America/Los_Angeles") == 3
    sql, params = conn.calls[0]
    assert "FROM campaign_events" in sql and "status = 'sent'" in sql
    assert "(at AT TIME ZONE %s)::date = %s" in sql
    assert params == ("America/Los_Angeles", date(2026, 10, 21))


def test_counts_by_week_pivots_rows_into_one_dict_per_week():
    rows = [
        {"week": date(2026, 10, 12), "kind": "companies_found", "n": 5},
        {"week": date(2026, 10, 12), "kind": "sent", "n": 2},
        {"week": date(2026, 10, 5), "kind": "above_bar", "n": 1},
    ]
    conn = FakeConn([rows])
    out = campaign.counts_by_week(conn, date(2026, 10, 1), date(2026, 10, 31))
    assert [r["week"] for r in out] == [date(2026, 10, 5), date(2026, 10, 12)]
    assert out[0]["above_bar"] == 1 and out[0]["sent"] == 0 and out[0]["companies_found"] == 0
    assert out[1]["companies_found"] == 5 and out[1]["sent"] == 2
    assert "bounced" in out[1] and "booked" in out[1]
    sql, params = conn.calls[0]
    assert "date_trunc('week'" in sql and "campaign_companies" in sql and "campaign_events" in sql
    assert "status = 'active'" in sql
    assert params.count(date(2026, 10, 1)) >= 1 and params.count(date(2026, 10, 31)) >= 1


def test_counts_by_week_without_bounds_has_no_date_filter():
    conn = FakeConn([[]])
    assert campaign.counts_by_week(conn, None, None) == []
    assert conn.calls[0][1] == ()


def test_set_people_bounced():
    conn = FakeConn()
    campaign.set_people_bounced(conn, "A@x.io", TS)
    sql, params = conn.calls[0]
    assert "UPDATE people SET email_bounced_at = COALESCE(email_bounced_at, %s)" in sql
    assert "WHERE lower(email) = lower(%s)" in sql
    assert params == (TS, "A@x.io")


def test_people_in_outreach_by_domain_joins_on_the_lower_cased_domain():
    conn = FakeConn([[{"id": 7, "email": "ceo@acme.com"}]])
    rows = campaign.people_in_outreach_by_domain(conn, "Acme.com")
    assert rows == [{"id": 7, "email": "ceo@acme.com"}]
    sql, params = conn.calls[0]
    assert "FROM people p JOIN outreach_contacts o ON o.person_id = p.id" in sql
    assert "WHERE lower(split_part(p.email, '@', 2)) = %s" in sql
    assert params == ("acme.com",)


def test_last_event_at_takes_the_latest_of_the_status():
    conn = FakeConn([[{"at": TS}]])
    assert campaign.last_event_at(conn, 4, "approved") == TS
    sql, params = conn.calls[0]
    assert sql == (
        "SELECT max(at) AS at FROM campaign_events WHERE contact_id = %s AND status = %s"
    )
    assert params == (4, "approved")


def test_bounced_count_for_run_joins_the_runs_contacts():
    conn = FakeConn([[{"n": 3}]])
    assert campaign.bounced_count_for_run(conn, 9) == 3
    sql, params = conn.calls[0]
    assert "JOIN campaign_contacts c ON c.id = e.contact_id" in sql
    assert "c.run_id = %s AND e.status = 'bounced'" in sql
    assert params == (9,)


def test_status_counts_for_run_fills_every_status():
    conn = FakeConn([[{"status": "sent", "n": 2}, {"status": "approved", "n": 1}]])
    counts = campaign.status_counts_for_run(conn, 9)
    assert counts["sent"] == 2 and counts["approved"] == 1 and counts["bounced"] == 0
    assert set(counts) == set(CONTACT_STATUSES)
    assert conn.calls[0][1] == (9,)


def test_attention_contacts_selects_unknown_sends_and_interrupted_follow_ups():
    conn = FakeConn([[{"id": 1}]])
    assert campaign.attention_contacts(conn) == [{"id": 1}]
    sql, params = conn.calls[0]
    assert "JOIN campaign_companies co ON co.id = c.company_id" in sql
    assert "ORDER BY at DESC, id DESC LIMIT 1" in sql
    assert "c.status = 'approved' AND c.draft_id IS NOT NULL" in sql
    assert "e.note = 'follow-up: sending'" in sql and "LIKE 'follow-up: FAILED%%'" in sql
    assert params is None
