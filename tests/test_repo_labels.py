from repo import labels
from tests.test_repo_people import FakeConn


def test_replace_groups_upserts_each_then_deletes_the_rest():
    conn = FakeConn()
    labels.replace_groups(conn, {"contactGroups/a": "Climbing", "contactGroups/b": "investor"})
    sqls = [s for s, _ in conn.calls]
    assert sum("INSERT INTO contact_groups" in s for s in sqls) == 2
    assert "ON CONFLICT (resource_name) DO UPDATE SET name = EXCLUDED.name" in sqls[0]
    assert conn.calls[0][1] == ("contactGroups/a", "Climbing")
    delete_sql, delete_params = conn.calls[-1]
    assert "DELETE FROM contact_groups" in delete_sql and "ANY(%s::text[])" in delete_sql
    assert delete_params == (["contactGroups/a", "contactGroups/b"],)


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
