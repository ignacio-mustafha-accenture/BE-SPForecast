from fastapi import APIRouter, Request
from app.dependencies import require_permission
from app.models.ppa import PPACreate
from app.services import ppa_service

router = APIRouter()


@router.get("", dependencies=[require_permission("ppa:read")])
async def list_ppa():
    return await ppa_service.list_ppa()


@router.post("", status_code=201, dependencies=[require_permission("ppa:create")])
async def create_ppa(body: PPACreate, request: Request):
    user = request.state.user
    return await ppa_service.create(body, user.get("eid"), request.state.request_id)
