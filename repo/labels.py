"""contact_groups and people_labels: the label index (multiple-labels design §4).
Takes an open connection; never opens one."""

from typing import Any


def replace_groups(conn: Any, groups: dict[str, str]) -> None:
    """Make contact_groups exactly `groups` (rn -> name). A group no longer
    listed is deleted, and its people_labels rows cascade with it."""
    for rn, name in groups.items():
        conn.execute(
            """
            INSERT INTO contact_groups (resource_name, name) VALUES (%s, %s)
            ON CONFLICT (resource_name) DO UPDATE SET name = EXCLUDED.name, updated_at = now()
            WHERE contact_groups.name IS DISTINCT FROM EXCLUDED.name
            """,
            (rn, name),
        )
    conn.execute(
        "DELETE FROM contact_groups WHERE NOT (resource_name = ANY(%s::text[]))",
        (list(groups),),
    )


def set_contact_labels(conn: Any, resource_name: str, group_rns: list[str]) -> None:
    """Replace the labels of the person linked to `resource_name`. An rn not in
    contact_groups (a system group, GOOGLE_CONTACT_GROUP, or a group newer than
    the last refresh) is dropped by the join — never an FK error that would
    abort the nightly sync."""
    conn.execute(
        """
        DELETE FROM people_labels
        WHERE person_id IN (SELECT id FROM people WHERE google_resource_name = %s)
        """,
        (resource_name,),
    )
    conn.execute(
        """
        INSERT INTO people_labels (person_id, group_resource_name)
        SELECT p.id, g.resource_name
        FROM people p JOIN contact_groups g ON g.resource_name = ANY(%s::text[])
        WHERE p.google_resource_name = %s
        ON CONFLICT DO NOTHING
        """,
        (group_rns, resource_name),
    )


def list_with_counts(conn: Any) -> list[dict]:
    return conn.execute(
        """
        SELECT g.name, count(pl.person_id)::int AS count
        FROM contact_groups g
        LEFT JOIN people_labels pl ON pl.group_resource_name = g.resource_name
        GROUP BY g.resource_name, g.name
        ORDER BY lower(g.name), g.name
        """
    ).fetchall()


def find_by_name(conn: Any, name: str) -> list[dict]:
    return conn.execute(
        "SELECT resource_name, name FROM contact_groups WHERE lower(name) = lower(%s)",
        (name,),
    ).fetchall()
