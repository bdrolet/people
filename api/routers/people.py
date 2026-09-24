from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from api.routers.imessage import IMessageSummary
from api.routers.linkedin import LinkedInSummary
from clients import db
from repo import imessage as imessage_repo
from repo import linkedin as linkedin_repo
from repo import people
from services import google_contacts_sync, person_edit
from services.eligibility import normalize

router = APIRouter()


class PersonOut(BaseModel):
    email: str
    display_name: str | None
    first_seen: datetime | None
    last_seen: datetime | None
    last_contacted: datetime | None
    message_count: int
    my_response_count: int
    relationship_label: str | None
    notes: str | None
    eligible: bool
    automated: bool
    in_google_contacts: bool
    in_hubspot: bool
    linkedin: LinkedInSummary | None = None
    imessage: IMessageSummary | None = None
    phone_numbers: list[str] = []
    company: str | None = None
    job_title: str | None = None
    contact: dict | None = None


class PersonList(BaseModel):
    results: list[PersonOut]


class PersonPatch(BaseModel):
    notes: str | None = None
    relationship_label: str | None = None
    contact: dict | None = None

    @field_validator("relationship_label")
    @classmethod
    def _relationship_label_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("relationship_label must not be blank")
        return v


def to_out(
    row: dict,
    linkedin_row: dict | None = None,
    imessage_row: dict | None = None,
    include_contact: bool = False,
) -> PersonOut:
    return PersonOut(
        email=row["email"],
        display_name=row.get("display_name"),
        first_seen=row.get("first_seen"),
        last_seen=row.get("last_seen"),
        last_contacted=row.get("last_contacted"),
        message_count=row.get("message_count") or 0,
        my_response_count=row.get("my_response_count") or 0,
        relationship_label=row.get("relationship_label"),
        notes=row.get("notes"),
        eligible=bool(row.get("eligible")),
        automated=bool(row.get("automated")),
        in_google_contacts=bool(row.get("google_resource_name")),
        in_hubspot=bool(row.get("hubspot_contact_id")),
        linkedin=LinkedInSummary.model_validate(linkedin_row) if linkedin_row else None,
        imessage=IMessageSummary.model_validate(imessage_row) if imessage_row else None,
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


@router.get("/people/{email}", response_model=PersonOut)
def get_person(email: str) -> PersonOut:
    email = normalize(email)
    with db.get_conn() as conn:
        row = people.get(conn, email)
        linkedin_row = linkedin_repo.connection_for_person(conn, email) if row else None
        imessage_row = imessage_repo.summary_for_person(conn, email) if row else None
    if row is None:
        raise HTTPException(status_code=404)
    return to_out(row, linkedin_row, imessage_row, include_contact=True)


@router.patch("/people/{email}", response_model=PersonOut)
def patch_person(email: str, body: PersonPatch) -> PersonOut:
    email = normalize(email)
    try:
        with db.get_conn() as conn:
            row = person_edit.update(
                conn,
                email,
                notes=body.notes,
                relationship_label=body.relationship_label,
                contact=body.contact,
            )
            conn.commit()
            linkedin_row = linkedin_repo.connection_for_person(conn, email)
            imessage_row = imessage_repo.summary_for_person(conn, email)
    except person_edit.NotFound:
        raise HTTPException(status_code=404)
    except person_edit.NotLinked:
        raise HTTPException(status_code=409, detail="person has no Google contact")
    except person_edit.Invalid as e:
        raise HTTPException(status_code=400, detail=str(e))
    except person_edit.Conflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    return to_out(row, linkedin_row, imessage_row, include_contact=True)


@router.post("/people/{email}/sync", response_model=PersonOut)
def sync_person(email: str) -> PersonOut:
    with db.get_conn() as conn:
        row = people.get(conn, normalize(email))
        if row is None:
            raise HTTPException(status_code=404)
        row = google_contacts_sync.sync_one(conn, row)
        conn.commit()
    return to_out(row, include_contact=True)
