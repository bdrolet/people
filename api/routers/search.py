from fastapi import APIRouter
from pydantic import BaseModel, Field

from api.routers.linkedin import LinkedInConnectionOut
from api.routers.people import PersonList, to_out
from clients import db
from repo import linkedin, people

router = APIRouter()


class SearchRequest(BaseModel):
    q: str
    limit: int = Field(default=20, ge=1, le=100)


class SearchResponse(PersonList):
    linkedin_results: list[LinkedInConnectionOut]


@router.post("/search", response_model=SearchResponse)
def search(body: SearchRequest) -> SearchResponse:
    with db.get_conn() as conn:
        results = [to_out(r) for r in people.search(conn, body.q, body.limit)]
        connections = linkedin.search_connections(conn, body.q, body.limit)
    return SearchResponse(
        results=results,
        linkedin_results=[LinkedInConnectionOut.model_validate(r) for r in connections],
    )
