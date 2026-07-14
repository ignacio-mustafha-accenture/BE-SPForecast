from fastapi import APIRouter, Query, Request
from app.dependencies import require_permission
from app.models.tickets import TicketCreate, TicketUpdate, TicketAssignEID, RejectPayload
from app.services import ticket_service

router = APIRouter()


@router.get("", dependencies=[require_permission("tickets:read")])
async def list_tickets(
    request: Request,
    status: str | None = Query(None),
    type: str | None = Query(None, alias="type"),
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    request.state.action = "List tickets"
    return await ticket_service.list_tickets(status=status, type_=type, q=q, page=page, page_size=page_size)


@router.post("", status_code=201, dependencies=[require_permission("tickets:create")])
async def create_ticket(body: TicketCreate, request: Request):
    request.state.action = f"Create ticket: {body.type}"
    user = request.state.user
    return await ticket_service.create(body, user.get("eid"), request.state.request_id)


@router.patch("/{ticket_id}/approve", dependencies=[require_permission("tickets:approve")])
async def approve_ticket(ticket_id: int, request: Request):
    request.state.action = f"Approve ticket #{ticket_id}"
    return await ticket_service.approve_ticket(ticket_id, request.state.request_id)


@router.patch("/{ticket_id}/reject", dependencies=[require_permission("tickets:approve")])
async def reject_ticket(ticket_id: int, body: RejectPayload, request: Request):
    request.state.action = f"Reject ticket #{ticket_id}"
    return await ticket_service.reject_ticket(ticket_id, body.reason, request.state.request_id)


@router.patch("/{ticket_id}", dependencies=[require_permission("tickets:update")])
async def update_ticket(ticket_id: int, body: TicketUpdate, request: Request):
    request.state.action = f"Update ticket #{ticket_id}"
    return await ticket_service.update(ticket_id, body, request.state.request_id)


@router.patch("/{ticket_id}/eid", dependencies=[require_permission("tickets:assign_eid")])
async def assign_eid(ticket_id: int, body: TicketAssignEID, request: Request):
    request.state.action = f"Assign EID to ticket #{ticket_id}"
    return await ticket_service.assign_eid(
        ticket_id, body.new_eid, body.new_name, request.state.request_id
    )
