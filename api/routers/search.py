from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.auth import verify_token
from api.routers.imessage import IMessageHandleOut
from api.routers.linkedin import LinkedInConnectionOut
from api.routers.people import PersonList, to_out
from clients import db
from repo import imessage, linkedin, people

router = APIRouter(dependencies=[Depends(verify_token)])


class SearchRequest(BaseModel):
    q: str
    limit: int = Field(default=20, ge=1, le=100)


class SearchResponse(PersonList):
    linkedin_results: list[LinkedInConnectionOut]
    imessage_results: list[IMessageHandleOut] = []


@router.post("/search", response_model=SearchResponse)
def search(body: SearchRequest) -> SearchResponse:
    with db.get_conn() as conn:
        results = [to_out(r) for r in people.search(conn, body.q, body.limit)]
        connections = linkedin.search_connections(conn, body.q, body.limit)
        handles = imessage.search_handles(conn, body.q, body.limit)
    return SearchResponse(
        results=results,
        linkedin_results=[LinkedInConnectionOut.model_validate(r) for r in connections],
        imessage_results=[IMessageHandleOut.model_validate(h) for h in handles],
    )
