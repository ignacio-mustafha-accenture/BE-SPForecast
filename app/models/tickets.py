from typing import Optional
from pydantic import BaseModel

VALID_TICKET_TYPES = {"newproj", "ongoing", "pto", "sick", "nj", "baja"}


class TicketCreate(BaseModel):
    type: str
    eid: Optional[str] = None
    detail: Optional[str] = None
    status: str = "Open"
    nj_name: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    new_end_date: Optional[str] = None
    cl: Optional[int] = None
    location: Optional[str] = None
    people_lead: Optional[str] = None
    client_name: Optional[str] = None
    offering_type: Optional[str] = None
    chargeability_pct: Optional[float] = None
    hours_to_move: Optional[int] = None
    from_period: Optional[str] = None
    to_period: Optional[str] = None
    comments: Optional[str] = None
    eid_accenture: Optional[str] = None


class TicketUpdate(BaseModel):
    status: Optional[str] = None
    detail: Optional[str] = None
    client_name: Optional[str] = None
    offering_type: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    chargeability_pct: Optional[float] = None
    hours_to_move: Optional[int] = None
    from_period: Optional[str] = None
    to_period: Optional[str] = None
    comments: Optional[str] = None
    rejection_reason: Optional[str] = None


class RejectPayload(BaseModel):
    reason: str


class TicketAssignEID(BaseModel):
    new_eid: str
    new_name: Optional[str] = None


class TicketOut(BaseModel):
    id: str
    type: str
    eid: Optional[str]
    detail: Optional[str]
    status: str
    date: Optional[str]
    nj_name: Optional[str] = None
    cl: Optional[int] = None
    location: Optional[str] = None
    people_lead: Optional[str] = None
    client_name: Optional[str] = None
    offering_type: Optional[str] = None
    chargeability_pct: Optional[float] = None
    hours_to_move: Optional[int] = None
    from_period: Optional[str] = None
    to_period: Optional[str] = None
    comments: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
