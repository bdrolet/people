from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from googleapiclient.errors import HttpError
from pydantic import BaseModel, ConfigDict

from api.routers.imessage import IMessageSummary
from api.routers.linkedin import LinkedInSummary
from api.routers.whatsapp import WhatsAppSummary
from clients import db
from repo import imessage as imessage_repo
from repo import linkedin as linkedin_repo
from repo import people
from repo import whatsapp as whatsapp_repo
from services import google_contacts_sync, identity, person_create, person_edit

router = APIRouter()


class PersonOut(BaseModel):
    id: int
    email: str | None
    display_name: str | None
    first_seen: datetime | None
    last_seen: datetime | None
    last_contacted: datetime | None
    message_count: int
    my_response_count: int
    labels: list[str] = []
    notes: str | None
    eligible: bool
    automated: bool
    in_google_contacts: bool
    in_hubspot: bool
    linkedin: LinkedInSummary | None = None
    imessage: IMessageSummary | None = None
    whatsapp: WhatsAppSummary | None = None
    phone_numbers: list[str] = []
    company: str | None = None
    job_title: str | None = None
    contact: dict | None = None


class PersonList(BaseModel):
    results: list[PersonOut]


class PersonCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact: dict
    notes: str | None = None
    labels: list[str] | None = None


class LabelChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    add: list[str] = []
    remove: list[str] = []


class PersonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str | None = None
    labels: LabelChange | None = None
    contact: dict | None = None


def to_out(
    row: dict,
    linkedin_row: dict | None = None,
    imessage_row: dict | None = None,
    whatsapp_row: dict | None = None,
    include_contact: bool = False,
) -> PersonOut:
    labels = row.get("labels") or []
    return PersonOut(
        id=row["id"],
        email=row["email"],
        display_name=row.get("display_name"),
        first_seen=row.get("first_seen"),
        last_seen=row.get("last_seen"),
        last_contacted=row.get("last_contacted"),
        message_count=row.get("message_count") or 0,
        my_response_count=row.get("my_response_count") or 0,
        labels=labels,
        notes=row.get("notes"),
        eligible=bool(row.get("eligible")),
        automated=bool(row.get("automated")),
        in_google_contacts=bool(row.get("google_resource_name")),
        in_hubspot=bool(row.get("hubspot_contact_id")),
        linkedin=LinkedInSummary.model_validate(linkedin_row) if linkedin_row else None,
        imessage=IMessageSummary.model_validate(imessage_row) if imessage_row else None,
        whatsapp=WhatsAppSummary.model_validate(whatsapp_row) if whatsapp_row else None,
        phone_numbers=row.get("phone_numbers") or [],
        company=row.get("company"),
        job_title=row.get("job_title"),
        contact=row.get("google_fields") if include_contact else None,
    )


@router.get("/people", response_model=PersonList)
def list_recent(
    recent: int = Query(default=20, ge=1, le=200), eligible_only: bool = True
) -> PersonList:
    with db.get_conn() as conn:
        return PersonList(results=[to_out(r) for r in people.recent(conn, recent, eligible_only)])


@router.post("/people", response_model=PersonOut, status_code=201)
def create_person(body: PersonCreate) -> PersonOut:
    try:
        with db.get_conn() as conn:
            row = person_create.create(
                conn,
                contact=body.contact,
                notes=body.notes,
                labels=body.labels,
            )
            conn.commit()
    except person_create.Invalid as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except person_create.Duplicate as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "person exists", "candidates": e.candidates},
        ) from e
    except person_edit.Invalid as e:
        raise HTTPException(status_code=400, detail=str(e))
    except person_edit.Conflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except HttpError as e:
        status = e.resp.status
        if 400 <= status < 500:
            raise HTTPException(status_code=400, detail=e.reason or "") from e
        raise
    return to_out(row, include_contact=True)


def resolve_person(conn: Any, ident: str) -> dict:
    """Classify `ident` — email, phone, or numeric id (spec §5.1) — and look
    up the matching row. Raises 404 when nothing can match, and 409 with the
    candidate ids when a phone number is shared by more than one person
    (ambiguity is an error, not a guess: never pick one)."""
    classified = identity.classify(ident)
    if classified is None:
        raise HTTPException(status_code=404)
    kind, value = classified
    row: dict | None
    if kind == "email":
        row = people.get(conn, str(value))
    elif kind == "id":
        row = people.get_by_id(conn, int(value))
    else:
        rows = people.get_by_phone(conn, str(value))
        if len(rows) > 1:
            raise HTTPException(
                status_code=409,
                detail={"error": "ambiguous phone", "candidates": [r["id"] for r in rows]},
            )
        row = rows[0] if rows else None
    if row is None:
        raise HTTPException(status_code=404)
    return row


@router.get("/people/{ident}", response_model=PersonOut)
def get_person(ident: str) -> PersonOut:
    with db.get_conn() as conn:
        row = resolve_person(conn, ident)
        linkedin_row = linkedin_repo.connection_for_person(conn, row["id"])
        imessage_row = imessage_repo.summary_for_person(conn, row["id"])
        whatsapp_row = whatsapp_repo.summary_for_person(conn, row["id"])
    return to_out(row, linkedin_row, imessage_row, whatsapp_row, include_contact=True)


@router.patch("/people/{ident}", response_model=PersonOut)
def patch_person(ident: str, body: PersonPatch) -> PersonOut:
    try:
        with db.get_conn() as conn:
            target = resolve_person(conn, ident)
            row = person_edit.update(
                conn,
                target["id"],
                notes=body.notes,
                labels=body.labels.model_dump() if body.labels is not None else None,
                contact=body.contact,
            )
            conn.commit()
            linkedin_row = linkedin_repo.connection_for_person(conn, row["id"])
            imessage_row = imessage_repo.summary_for_person(conn, row["id"])
            whatsapp_row = whatsapp_repo.summary_for_person(conn, row["id"])
    except person_edit.NotFound:
        raise HTTPException(status_code=404)
    except person_edit.NotLinked:
        raise HTTPException(status_code=409, detail="person has no Google contact")
    except person_edit.Invalid as e:
        raise HTTPException(status_code=400, detail=str(e))
    except person_edit.Conflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    return to_out(row, linkedin_row, imessage_row, whatsapp_row, include_contact=True)


@router.post("/people/{ident}/sync", response_model=PersonOut)
def sync_person(ident: str) -> PersonOut:
    with db.get_conn() as conn:
        row = resolve_person(conn, ident)
        row = google_contacts_sync.sync_one(conn, row)
        conn.commit()
    return to_out(row, include_contact=True)
