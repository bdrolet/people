"""Find and verify a contact's email for the funded-founder campaign. Clients are
injected (the apollo and hunter modules) so tests need no network."""

import logging
import re
import unicodedata

import httpx

from clients.apollo import ApolloUnavailable

log = logging.getLogger(__name__)

_RANK = {"valid": 2, "accept_all": 1, "unknown": 0}


def _clean(token: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", token).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_.lower())


def _apply_pattern(pattern: str, first: str, last: str, domain: str) -> str | None:
    first, last = _clean(first), _clean(last)
    if not first:
        return None
    if ("{last}" in pattern or "{l}" in pattern) and not last:
        return None
    out = pattern
    for token, val in {
        "{first}": first,
        "{last}": last,
        "{f}": first[:1],
        "{l}": last[:1],
    }.items():
        out = out.replace(token, val)
    if not re.fullmatch(r"[a-z0-9._-]+", out):
        return None
    return f"{out}@{domain}"


def _well_formed(email: str) -> bool:
    return email.count("@") == 1 and not re.search(r"\s", email)


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
        try:
            email = _apollo_email(contact, company, name, apollo)
        except ApolloUnavailable:
            log.warning("Apollo is unavailable (no key); continuing with Hunter")
            email = None
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (401, 403):
                raise  # 5xx/429 are outages, not plan limits
            log.warning(
                "Apollo is unavailable on this plan/key (HTTP %s); continuing with Hunter",
                e.response.status_code,
            )
            email = None
        if email:
            yield email, "apollo"
    found = hunter.find(company["domain"], first, last) if last else None
    if found and found.get("email"):
        yield found["email"], "hunter"
    pattern = hunter.domain_pattern(company["domain"])
    if pattern:
        guess = _apply_pattern(pattern, first, last, company["domain"])
        if guess:
            yield guess, "hunter"


def resolve_email(
    contact: dict, company: dict, *, apollo, hunter
) -> tuple[str | None, str, str | None]:
    tokens = contact["full_name"].split()
    first = tokens[0] if tokens else ""
    last = tokens[-1] if len(tokens) >= 2 else ""
    seen: set[str] = set()
    best: tuple[str, str, str] | None = None
    for email, source in _candidates(contact, company, first, last, apollo, hunter):
        key = email.lower()
        if not _well_formed(email) or key in seen:
            continue
        seen.add(key)
        status = hunter.verify(email)
        if status == "valid":
            return email, "valid", source
        if status != "invalid" and (best is None or _RANK[status] > _RANK[best[1]]):
            best = (email, status, source)
    return best if best else (None, "unknown", None)
