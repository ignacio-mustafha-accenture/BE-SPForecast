from typing import Optional
from pydantic import BaseModel


class PPACreate(BaseModel):
    eid: str
    from_period: str
    to_period: str
    hours: int
    reason: Optional[str] = None


class PPAOut(BaseModel):
    id: str
    eid: str
    name: Optional[str]
    from_period: str
    to_period: str
    hours: int
    reason: Optional[str]
    date: Optional[str]
