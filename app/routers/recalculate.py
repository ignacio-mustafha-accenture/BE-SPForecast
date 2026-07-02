from fastapi import APIRouter, Request
from app.dependencies import require_permission
from app.services import recalculate_service

router = APIRouter()


@router.post("/employee/{eid}", dependencies=[require_permission("recalculate:employee")])
async def recalculate_employee(eid: str, request: Request):
    return await recalculate_service.recalculate_employee(eid, request.state.request_id)


@router.post("/{period_name}", dependencies=[require_permission("recalculate:period")])
async def recalculate_period(period_name: str, request: Request):
    return await recalculate_service.recalculate_period(period_name, request.state.request_id)
