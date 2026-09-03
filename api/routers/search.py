from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.auth import verify_token
from api.routers.people import PersonList, to_out
from clients import db
from repo import people

router = APIRouter(dependencies=[Depends(verify_token)])


class SearchRequest(BaseModel):
    q: str
    limit: int = Field(default=20, ge=1, le=100)


@router.post("/search", response_model=PersonList)
def search(body: SearchRequest) -> PersonList:
    with db.get_conn() as conn:
        return PersonList(results=[to_out(r) for r in people.search(conn, body.q, body.limit)])
