from fastapi import APIRouter, Query, Request
from app.dependencies import require_permission
from app.models.ppa import PPACreate
from app.services import ppa_service

router = APIRouter()


@router.get("", dependencies=[require_permission("ppa:read")])
async def list_ppa(
    request: Request,
    eid: str | None = Query(None),
    from_period: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    request.state.action = "List PPA"
    return await ppa_service.list_ppa(eid=eid, from_period=from_period, page=page, page_size=page_size)


@router.post("", status_code=201, dependencies=[require_permission("ppa:create")])
async def create_ppa(body: PPACreate, request: Request):
    request.state.action = f"Create PPA: {body.eid}"
    user = request.state.user
    return await ppa_service.create(body, user.get("eid"), request.state.request_id)
