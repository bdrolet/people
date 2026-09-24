from datetime import UTC, datetime

from repo import people


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Records every (sql, params) and returns canned rows in order."""

    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple | None]] = []
        self._results = list(results or [])

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        rows = self._results.pop(0) if self._results else []
        return FakeCursor(rows)


TS = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def test_upsert_inbound_lowercases_and_increments():
    conn = FakeConn(results=[[{"email": "alice@example.com", "message_count": 2}]])
    row = people.upsert_inbound(conn, "Alice@Example.com", "Alice", TS)
    sql, params = conn.calls[0]
    assert "INSERT INTO people" in sql and "ON CONFLICT (email)" in sql
    assert "message_count = people.message_count + 1" in sql
    assert "RETURNING" in sql
    assert params[0] == "alice@example.com"
    assert row["message_count"] == 2


def test_upsert_inbound_keeps_existing_display_name():
    conn = FakeConn(results=[[{"email": "a@b.c"}]])
    people.upsert_inbound(conn, "a@b.c", "New Name", TS)
    sql, _ = conn.calls[0]
    assert "display_name = COALESCE(people.display_name, EXCLUDED.display_name)" in sql


def test_upsert_outbound_increments_response_count():
    conn = FakeConn(results=[[{"email": "a@b.c"}]])
    people.upsert_outbound(conn, "a@b.c", None, TS)
    sql, _ = conn.calls[0]
    assert "my_response_count = people.my_response_count + 1" in sql
    assert "last_contacted = GREATEST" in sql


def test_set_flags_is_monotonic_on_eligible():
    conn = FakeConn()
    people.set_flags(conn, "a@b.c", automated=False, eligible=True)
    sql, params = conn.calls[0]
    assert "eligible = people.eligible OR %s" in sql
    assert params == (False, True, "a@b.c")


def test_search_uses_trigram_and_email_match():
    conn = FakeConn(results=[[]])
    people.search(conn, "ali", 10)
    sql, params = conn.calls[0]
    assert "email ILIKE %s" in sql and "similarity(display_name, %s)" in sql
    assert params[0] == "%ali%"


def test_oldest_managed_orders_by_last_interaction_asc():
    conn = FakeConn(results=[[{"email": "old@b.c"}]])
    row = people.oldest_managed(conn)
    sql, _ = conn.calls[0]
    assert "hubspot_contact_id IS NOT NULL" in sql and "ASC LIMIT 1" in sql
    assert row["email"] == "old@b.c"


def test_mark_google_deleted_clears_resource_name():
    conn = FakeConn()
    people.mark_google_deleted(conn, "people/c1")
    sql, params = conn.calls[0]
    assert "google_deleted_at = now()" in sql and "google_resource_name = NULL" in sql
    assert params == ("people/c1",)


def test_rows_for_imessage_matching_selects_google_link():
    conn = FakeConn(results=[[]])
    people.rows_for_imessage_matching(conn)
    sql, _ = conn.calls[0]
    assert "email" in sql and "display_name" in sql and "google_resource_name" in sql
    assert "FROM people" in sql


def test_update_from_google_persists_contact_fields():
    conn = FakeConn()
    people.update_from_google(
        conn,
        "people/c1",
        etag="e1",
        display_name="Alice",
        notes=None,
        relationship_label=None,
        phone_numbers=["+15550100001"],
        company="Example Health",
        job_title="CTO",
        google_fields={"names": [{"givenName": "Alice"}]},
    )
    sql, params = conn.calls[0]
    assert "phone_numbers = %s::text[]" in sql and "google_fields = %s::jsonb" in sql
    assert ["+15550100001"] in params
    assert "Example Health" in params and "CTO" in params
    assert {"names": [{"givenName": "Alice"}]} in params


def test_columns_include_contact_fields():
    assert "phone_numbers" in people._COLUMNS and "google_fields" in people._COLUMNS
