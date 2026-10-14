"""Resolve an email, LinkedIn profile URL, E.164 phone, or numeric id to one people row, for scripts
(outreach spec §7.4: `<person>` resolves the way people-api does). The rule is
services/identity.py's; ambiguity is an error with the candidate ids, never a guess."""

from typing import Any

from repo import people
from services import identity


class NotFound(LookupError):
    pass


class Ambiguous(LookupError):
    def __init__(self, ident: str, candidates: list[int]) -> None:
        self.candidates = candidates
        super().__init__(f"{ident} matches several people: {candidates}")


def resolve(conn: Any, ident: str) -> dict:
    classified = identity.classify(ident)
    if classified is None:
        raise NotFound(ident)
    kind, value = classified
    if kind == "email":
        row = people.get(conn, str(value))
    elif kind == "linkedin":
        row = people.get_by_linkedin_url(conn, str(value))
    elif kind == "id":
        row = people.get_by_id(conn, int(value))
    else:
        rows = people.get_by_phone(conn, str(value))
        if len(rows) > 1:
            raise Ambiguous(ident, [r["id"] for r in rows])
        row = rows[0] if rows else None
    if row is None:
        raise NotFound(ident)
    return row
