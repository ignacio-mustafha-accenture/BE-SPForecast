from fastapi import APIRouter, Query, Request
from app.dependencies import require_permission
from app.models.ppa import PPACreate, PPAReject
from app.services import ppa_service

router = APIRouter()


@router.get("", dependencies=[require_permission("ppa:read")])
async def list_ppa(
    request: Request,
    eid: str | None = Query(None),
    from_period: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    request.state.action = "List PPA"
    return await ppa_service.list_ppa(
        eid=eid, from_period=from_period, status=status, page=page, page_size=page_size
    )


@router.post("", status_code=201, dependencies=[require_permission("ppa:create")])
async def create_ppa(body: PPACreate, request: Request):
    request.state.action = f"Create PPA: {body.eid}"
    user = request.state.user
    actor = user.get("eid") or user.get("email")
    return await ppa_service.create(body, actor, request.state.request_id)


@router.get("/{ppa_id}", dependencies=[require_permission("ppa:read")])
async def get_ppa(ppa_id: str, request: Request):
    request.state.action = f"Get PPA: {ppa_id}"
    return await ppa_service.get_by_id(ppa_id)


@router.post("/{ppa_id}/approve", dependencies=[require_permission("ppa:approve")])
async def approve_ppa(ppa_id: str, request: Request):
    request.state.action = f"Approve PPA: {ppa_id}"
    user = request.state.user
    actor = user.get("eid") or user.get("email")
    return await ppa_service.approve(ppa_id, actor, request.state.request_id)


@router.post("/{ppa_id}/reject", dependencies=[require_permission("ppa:reject")])
async def reject_ppa(ppa_id: str, body: PPAReject, request: Request):
    request.state.action = f"Reject PPA: {ppa_id}"
    user = request.state.user
    actor = user.get("eid") or user.get("email")
    return await ppa_service.reject(ppa_id, body.reason, actor, request.state.request_id)


@router.post("/{ppa_id}/reverse", dependencies=[require_permission("ppa:reverse")])
async def reverse_ppa(ppa_id: str, request: Request):
    request.state.action = f"Reverse PPA: {ppa_id}"
    user = request.state.user
    actor = user.get("eid") or user.get("email")
    return await ppa_service.reverse(ppa_id, actor, request.state.request_id)
