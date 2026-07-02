from typing import Optional
from pydantic import BaseModel


class EmployeeUpdate(BaseModel):
    new_eid: Optional[str] = None
    name: Optional[str] = None
    cl: Optional[float] = None
    client: Optional[str] = None
    offering: Optional[str] = None
    roll_on: Optional[str] = None
    roll_off: Optional[str] = None
    account_manager: Optional[str] = None
    notes: Optional[str] = None
    next_client: Optional[str] = None
    chargeability_pct: Optional[float] = None
