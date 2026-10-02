from repo import labels
from tests.test_repo_people import FakeConn


def _writes(conn):
    return [(s, p) for s, p in conn.calls if not s.startswith("SELECT")]


def test_replace_groups_unchanged_makes_no_writes():
    conn = FakeConn(results=[[{"resource_name": "contactGroups/a", "name": "Climbing"}]])
    labels.replace_groups(conn, {"contactGroups/a": "Climbing"})
    assert conn.calls[0][0].startswith("SELECT resource_name, name FROM contact_groups")
    assert _writes(conn) == []


def test_replace_groups_inserts_only_new():
    conn = FakeConn(results=[[{"resource_name": "contactGroups/a", "name": "Climbing"}]])
    labels.replace_groups(conn, {"contactGroups/a": "Climbing", "contactGroups/b": "investor"})
    [(sql, params)] = _writes(conn)
    assert "INSERT INTO contact_groups" in sql
    assert params == ("contactGroups/b", "investor")


def test_replace_groups_updates_only_renamed():
    conn = FakeConn(results=[[{"resource_name": "contactGroups/a", "name": "Climbing"}]])
    labels.replace_groups(conn, {"contactGroups/a": "Bouldering"})
    [(sql, params)] = _writes(conn)
    assert "UPDATE contact_groups" in sql
    assert params == ("Bouldering", "contactGroups/a")


def test_replace_groups_deletes_only_removed():
    conn = FakeConn(
        results=[
            [
                {"resource_name": "contactGroups/a", "name": "Climbing"},
                {"resource_name": "contactGroups/b", "name": "investor"},
            ]
        ]
    )
    labels.replace_groups(conn, {"contactGroups/a": "Climbing"})
    [(sql, params)] = _writes(conn)
    assert "DELETE FROM contact_groups" in sql and "ANY(%s::text[])" in sql
    assert params == (["contactGroups/b"],)


def test_set_contact_labels_replaces_by_resource_name():
    conn = FakeConn()
    labels.set_contact_labels(conn, "people/c1", ["contactGroups/a"])
    (del_sql, del_params), (ins_sql, ins_params) = conn.calls
    assert "DELETE FROM people_labels" in del_sql and del_params == ("people/c1",)
    assert "INSERT INTO people_labels" in ins_sql and "JOIN contact_groups" in ins_sql
    assert ins_params == (["contactGroups/a"], "people/c1")


def test_list_with_counts_left_joins_so_empty_labels_appear():
    conn = FakeConn(results=[[{"name": "Climbing", "count": 0}]])
    assert labels.list_with_counts(conn) == [{"name": "Climbing", "count": 0}]
    assert "LEFT JOIN people_labels" in conn.calls[0][0]


def test_find_by_name_is_case_insensitive():
    conn = FakeConn(results=[[]])
    labels.find_by_name(conn, "Climbing")
    sql, params = conn.calls[0]
    assert "lower(name) = lower(%s)" in sql and params == ("Climbing",)
