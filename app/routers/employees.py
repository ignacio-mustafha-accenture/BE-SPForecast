from fastapi import APIRouter, Query, Request
from app.dependencies import require_permission
from app.models.employees import AssignEidBody, EmployeeUpdate
from app.services import employee_service

router = APIRouter()


@router.get("", dependencies=[require_permission("state:read")])
async def list_employees(
    request: Request,
    country: str | None = Query(None),
    cl: str | None = Query(None),
    q: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    offering: str | None = Query(None),
    te_approver: str | None = Query(None),
    chg_bucket: str | None = Query(None),
):
    request.state.action = "List employees"
    return await employee_service.list_employees(
        country, cl, q, status, page, page_size,
        offering=offering, te_approver=te_approver, chg_bucket=chg_bucket,
    )

@router.get("/on-pto", dependencies=[require_permission("state:read")])
async def employees_on_pto(request: Request):
    request.state.action = "List employees on PTO"
    return await employee_service.get_employees_on_pto()


@router.get("/{eid}", dependencies=[require_permission("state:read")])
async def get_employee(eid: str, request: Request):
    request.state.action = f"View employee: {eid}"
    return await employee_service.get_employee(eid)


@router.patch("/{eid}/assign-eid", dependencies=[require_permission("employees:update")])
async def assign_nj_eid(eid: str, body: AssignEidBody, request: Request):
    request.state.action = f"Assign EID to NJ: {eid}"
    return await employee_service.assign_real_eid(eid, body.new_eid, body.new_name, request.state.request_id)


@router.patch("/{eid}", dependencies=[require_permission("employees:update")])
async def update_employee(eid: str, body: EmployeeUpdate, request: Request):
    request.state.action = f"Update employee: {eid}"
    return await employee_service.update(eid, body, request.state.request_id)
