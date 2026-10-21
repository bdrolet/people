"""Find and verify a contact's email for the funded-founder campaign. Clients are
injected (the apollo and hunter modules) so tests need no network."""

_RANK = {"valid": 2, "accept_all": 1, "unknown": 0}


def _apply_pattern(pattern: str, first: str, last: str, domain: str) -> str | None:
    if not first or not last:
        return None
    subs = {"{first}": first, "{last}": last, "{f}": first[0], "{l}": last[0]}
    out = pattern
    for token, val in subs.items():
        out = out.replace(token, val)
    if "{" in out:
        return None
    return f"{out}@{domain}"


def _apollo_email(contact: dict, company: dict, name: str, apollo) -> str | None:
    if contact.get("linkedin_url"):
        person = apollo.enrich(contact["linkedin_url"], contact.get("email"))
        return (person or {}).get("email")
    for p in apollo.people_search(company["domain"], []):
        if (p.get("name") or "").strip().lower() == name.lower():
            return p.get("email")
    return None


def _candidates(contact: dict, company: dict, first: str, last: str, apollo, hunter):
    """Yield (email, source) lazily, in priority order."""
    name = contact["full_name"].strip()
    if contact.get("email"):
        yield contact["email"], "manual"
    else:
        email = _apollo_email(contact, company, name, apollo)
        if email:
            yield email, "apollo"
    found = hunter.find(company["domain"], first, last)
    if found and found.get("email"):
        yield found["email"], "hunter"
    pattern = hunter.domain_pattern(company["domain"])
    if pattern:
        guess = _apply_pattern(pattern, first.lower(), last.lower(), company["domain"])
        if guess:
            yield guess, "hunter"


def resolve_email(
    contact: dict, company: dict, *, apollo, hunter
) -> tuple[str | None, str, str | None]:
    parts = contact["full_name"].strip().rsplit(" ", 1)
    first, last = (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")
    seen: set[str] = set()
    best: tuple[str, str, str] | None = None
    for email, source in _candidates(contact, company, first, last, apollo, hunter):
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        status = hunter.verify(email)
        if status == "valid":
            return email, "valid", source
        if status != "invalid" and (best is None or _RANK[status] > _RANK[best[1]]):
            best = (email, status, source)
    return best if best else (None, "unknown", None)
