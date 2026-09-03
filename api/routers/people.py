from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

from api.auth import verify_token
from clients import db
from repo import people
from services import google_contacts_sync, person_edit
from services.eligibility import normalize

router = APIRouter(dependencies=[Depends(verify_token)])


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


class PersonList(BaseModel):
    results: list[PersonOut]


class PersonPatch(BaseModel):
    notes: str | None = None
    relationship_label: str | None = None

    @field_validator("relationship_label")
    @classmethod
    def _relationship_label_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("relationship_label must not be blank")
        return v


def to_out(row: dict) -> PersonOut:
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
    )


@router.get("/people", response_model=PersonList)
def list_recent(
    recent: int = Query(default=20, ge=1, le=200), eligible_only: bool = True
) -> PersonList:
    with db.get_conn() as conn:
        return PersonList(results=[to_out(r) for r in people.recent(conn, recent, eligible_only)])


@router.get("/people/{email}", response_model=PersonOut)
def get_person(email: str) -> PersonOut:
    with db.get_conn() as conn:
        row = people.get(conn, normalize(email))
    if row is None:
        raise HTTPException(status_code=404)
    return to_out(row)


@router.patch("/people/{email}", response_model=PersonOut)
def patch_person(email: str, body: PersonPatch) -> PersonOut:
    try:
        with db.get_conn() as conn:
            row = person_edit.update(
                conn, normalize(email), notes=body.notes, relationship_label=body.relationship_label
            )
            conn.commit()
    except person_edit.NotFound:
        raise HTTPException(status_code=404)
    except person_edit.NotLinked:
        raise HTTPException(status_code=409, detail="person has no Google contact")
    return to_out(row)


@router.post("/people/{email}/sync", response_model=PersonOut)
def sync_person(email: str) -> PersonOut:
    with db.get_conn() as conn:
        row = people.get(conn, normalize(email))
        if row is None:
            raise HTTPException(status_code=404)
        row = google_contacts_sync.sync_one(conn, row)
        conn.commit()
    return to_out(row)
