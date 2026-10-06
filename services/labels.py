"""Google contact groups as labels (multiple-labels design). Pure — no I/O.

A label is a user-defined contact group. System groups (myContacts, starred,
Google's built-in friends/family/coworkers) and people's own
GOOGLE_CONTACT_GROUP are never labels."""

SYSTEM_GROUP_TYPE = "SYSTEM_CONTACT_GROUP"


class LabelError(Exception):
    """Blank, contradictory, or reserved label name. -> 400."""


class AmbiguousLabel(LabelError):
    """A name matches several groups case-insensitively and none exactly. -> 409."""


def _display(group: dict) -> str:
    return group.get("formattedName") or group.get("name") or ""


def user_groups(by_rn: dict[str, dict], reserved: str) -> dict[str, str]:
    """rn -> name for every group that can be a label."""
    return {
        rn: _display(g)
        for rn, g in by_rn.items()
        if g.get("groupType") != SYSTEM_GROUP_TYPE and _display(g).lower() != reserved.lower()
    }


def membership_rns(person: dict) -> list[str]:
    """Every contact-group rn on a Google person payload, in order, deduplicated.
    Unfiltered: the DB join (repo/labels.py::set_contact_labels) keeps only rns
    that are labels."""
    out: list[str] = []
    for m in person.get("memberships", []):
        rn = (m.get("contactGroupMembership") or {}).get("contactGroupResourceName")
        if rn and rn not in out:
            out.append(rn)
    return out


def _clean(names: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        s = (n or "").strip()
        if not s:
            raise LabelError("label names must not be blank")
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def normalize_change(add: list[str], remove: list[str]) -> tuple[list[str], list[str]]:
    """Strip names, collapse case-insensitive duplicates within a list, and
    reject blanks or a name in both lists (design §5.2)."""
    a, r = _clean(add), _clean(remove)
    both = {n.lower() for n in a} & {n.lower() for n in r}
    if both:
        raise LabelError(f"label in both add and remove: {', '.join(sorted(both))}")
    return a, r


def match_name(candidates: dict[str, str], name: str) -> str | None:
    """The rn whose name equals `name` ignoring case. An exact-case match wins a
    tie; several case-insensitive matches with no exact one is AmbiguousLabel."""
    hits = {rn: n for rn, n in candidates.items() if n.lower() == name.lower()}
    if not hits:
        return None
    exact = sorted(rn for rn, n in hits.items() if n == name)
    if exact:
        return exact[0]
    if len(hits) == 1:
        return next(iter(hits))
    raise AmbiguousLabel(
        f"label {name!r} matches {', '.join(sorted(hits.values()))}; use the exact spelling"
    )


def resolve(by_rn: dict[str, dict], name: str, reserved: str) -> str | None:
    """The user group `name` refers to, or None if no such label exists yet.
    A user group wins over a same-named system group; a name that matches only
    a system group, or the reserved group, is rejected."""
    if name.lower() == reserved.lower():
        raise LabelError(f"{reserved!r} is reserved for people's own use, not a label")
    rn = match_name(user_groups(by_rn, reserved), name)
    if rn is not None:
        return rn
    system = {
        n.lower()
        for g in by_rn.values()
        if g.get("groupType") == SYSTEM_GROUP_TYPE
        for n in (g.get("name"), g.get("formattedName"))
        if n
    }
    if name.lower() in system:
        raise LabelError(f"{name!r} is a built-in Google group, not a label")
    return None
