from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from app.dependencies import require_permission
from app.services import holiday_service

router = APIRouter()


@router.get("", dependencies=[require_permission("state:read")])
async def list_holidays(request: Request, country: str = Query(...)):
    request.state.action = f"List holidays: {country}"
    return {"holidays": await holiday_service.list_holidays(country)}


class HolidayCreate(BaseModel):
    country: str
    date: str
    name: str


@router.post("", dependencies=[require_permission("employees:update")])
async def create_holiday(body: HolidayCreate, request: Request):
    request.state.action = f"Add holiday: {body.name} ({body.country} {body.date})"
    holiday = await holiday_service.create_holiday(body.country, body.date, body.name)
    return holiday


@router.delete("/{holiday_id}", dependencies=[require_permission("employees:update")])
async def delete_holiday(holiday_id: int, request: Request):
    request.state.action = f"Delete holiday: {holiday_id}"
    await holiday_service.delete_holiday(holiday_id)
    return {"ok": True}
