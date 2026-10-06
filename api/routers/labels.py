"""Labels — Google contact groups (multiple-labels design §6.2). Served from
the DB alone; no Google calls on the read path."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.routers.people import PersonList, to_out
from clients import db
from repo import labels as labels_repo
from repo import people
from services import labels as label_rules

router = APIRouter()


class LabelOut(BaseModel):
    name: str
    count: int


class LabelList(BaseModel):
    results: list[LabelOut]


@router.get("/labels", response_model=LabelList)
def list_labels() -> LabelList:
    with db.get_conn() as conn:
        rows = labels_repo.list_with_counts(conn)
    return LabelList(results=[LabelOut.model_validate(r) for r in rows])


@router.get("/labels/{name:path}", response_model=PersonList)
def people_with_label(name: str) -> PersonList:
    with db.get_conn() as conn:
        candidates = {r["resource_name"]: r["name"] for r in labels_repo.find_by_name(conn, name)}
        try:
            rn = label_rules.match_name(candidates, name)
        except label_rules.AmbiguousLabel as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        if rn is None:
            raise HTTPException(status_code=404, detail=f"no label {name!r}")
        return PersonList(results=[to_out(r) for r in people.with_label(conn, rn)])
