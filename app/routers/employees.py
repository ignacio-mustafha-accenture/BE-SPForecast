from fastapi import APIRouter, Query, Request
from app.dependencies import require_permission
from app.models.employees import EmployeeUpdate
from app.services import employee_service

router = APIRouter()


@router.get("", dependencies=[require_permission("state:read")])
async def list_employees(
    request: Request,
    country: str | None = Query(None),
    q: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    request.state.action = "List employees"
    return await employee_service.list_employees(country, q, status, page, page_size)


@router.get("/{eid}", dependencies=[require_permission("state:read")])
async def get_employee(eid: str, request: Request):
    request.state.action = f"View employee: {eid}"
    return await employee_service.get_employee(eid)


@router.patch("/{eid}", dependencies=[require_permission("employees:update")])
async def update_employee(eid: str, body: EmployeeUpdate, request: Request):
    request.state.action = f"Update employee: {eid}"
    return await employee_service.update(eid, body, request.state.request_id)
