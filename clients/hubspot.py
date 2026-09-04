"""HubSpot CRM I/O (ported from inbox clients/hubspot.py). Every function
raises on failure; services/hubspot_mirror.py owns retry/swallow policy.
Owner id comes from HUBSPOT_OWNER_ID — no default (public repo)."""

import json
import logging
import os
from collections.abc import Iterator
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class HubSpotUnavailable(RuntimeError):
    pass


def _token() -> str:
    tok = os.environ.get("HUBSPOT_TOKEN", "")
    if not tok:
        raise HubSpotUnavailable("HUBSPOT_TOKEN unset")
    return tok


def _client():
    from hubspot import HubSpot

    return HubSpot(access_token=_token())


def _as_utc(dt: datetime) -> datetime:
    """Normalize datetime to UTC: naive → assume UTC; aware → convert to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _hs_date(dt: datetime) -> str:
    """HubSpot date property: midnight UTC in ms."""
    dt_utc = _as_utc(dt)
    return str(int(dt_utc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000))


def _split_name(display_name: str | None) -> tuple[str, str]:
    parts = (display_name or "").strip().split(" ", 1)
    return (parts[0] if parts and parts[0] else ""), (parts[1] if len(parts) > 1 else "")


def find_by_email(email: str) -> dict | None:
    from hubspot.crm.contacts import PublicObjectSearchRequest

    result = _client().crm.contacts.search_api.do_search(
        PublicObjectSearchRequest(
            filter_groups=[
                {"filters": [{"value": email, "propertyName": "email", "operator": "EQ"}]}
            ],
            properties=["email", "last_email_date"],
            limit=1,
        )
    )
    if not result.results:
        return None
    hit = result.results[0]
    return {
        "id": hit.id,
        "email": hit.properties.get("email"),
        "last_email_date": hit.properties.get("last_email_date"),
    }


def create_contact(email: str, display_name: str | None, last_interaction: datetime | None) -> str:
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate

    first, last = _split_name(display_name)
    props = {
        "email": email,
        "lifecyclestage": "lead",
        "hs_lead_status": "NEW",
        "hubspot_owner_id": os.environ["HUBSPOT_OWNER_ID"],
    }
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if last_interaction:
        props["last_email_date"] = _hs_date(last_interaction)
    created = _client().crm.contacts.basic_api.create(
        SimplePublicObjectInputForCreate(properties=props)
    )
    return created.id


def update_last_email_date(contact_id: str, dt: datetime) -> None:
    from hubspot.crm.contacts import SimplePublicObjectInput

    _client().crm.contacts.basic_api.update(
        contact_id, SimplePublicObjectInput(properties={"last_email_date": _hs_date(dt)})
    )


def archive_contact(contact_id: str) -> None:
    """Soft delete — recoverable in HubSpot for 90 days."""
    _client().crm.contacts.basic_api.archive(contact_id)


def count_contacts() -> int:
    from hubspot.crm.contacts import PublicObjectSearchRequest

    result = _client().crm.contacts.search_api.do_search(PublicObjectSearchRequest(limit=1))
    return int(result.total)


def list_contacts() -> Iterator[dict]:
    """All contacts, paged. Yields {id, email, last_email_date}."""
    client = _client()
    after = None
    while True:
        page = client.crm.contacts.basic_api.get_page(
            limit=100, after=after, properties=["email", "last_email_date"]
        )
        for c in page.results:
            yield {
                "id": c.id,
                "email": (c.properties.get("email") or "").lower(),
                "last_email_date": c.properties.get("last_email_date"),
            }
        if not page.paging or not page.paging.next:
            return
        after = page.paging.next.after


def log_email(
    contact_id: str,
    subject: str,
    sender_email: str,
    body: str,
    received_at: datetime,
    body_html: str | None = None,
) -> None:
    from hubspot.crm import AssociationType
    from hubspot.crm.objects.emails import (
        AssociationSpec,
        PublicAssociationsForObject,
        PublicObjectId,
        SimplePublicObjectInputForCreate,
    )

    props = {
        "hs_timestamp": str(int(_as_utc(received_at).timestamp() * 1000)),
        "hs_email_subject": subject or "(no subject)",
        "hs_email_direction": "INCOMING_EMAIL",
        "hs_email_status": "SENT",
        "hs_email_headers": json.dumps({"from": {"email": sender_email}}),
    }
    if body_html:
        props["hs_email_html"] = body_html
    else:
        props["hs_email_text"] = body
    association = PublicAssociationsForObject(
        to=PublicObjectId(id=contact_id),
        types=[
            AssociationSpec(
                association_category="HUBSPOT_DEFINED",
                association_type_id=AssociationType.EMAIL_TO_CONTACT,
            )
        ],
    )
    _client().crm.objects.emails.basic_api.create(
        SimplePublicObjectInputForCreate(properties=props, associations=[association])
    )
