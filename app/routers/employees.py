from fastapi import APIRouter, Request
from app.dependencies import require_permission
from app.models.employees import EmployeeUpdate
from app.services import employee_service

router = APIRouter()


@router.patch("/{eid}", dependencies=[require_permission("employees:update")])
async def update_employee(eid: str, body: EmployeeUpdate, request: Request):
    return await employee_service.update(eid, body, request.state.request_id)
