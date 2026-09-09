from typing import Optional
from pydantic import BaseModel, model_validator


class PPACreate(BaseModel):
    eid: str
    from_period: str
    to_period: str
    hours_chargeable: Optional[int] = None
    hours_standard: Optional[int] = None
    reason: Optional[str] = None

    @model_validator(mode="after")
    def at_least_one_hours_field(self):
        if not self.hours_chargeable and not self.hours_standard:
            raise ValueError("At least one of hours_chargeable or hours_standard is required")
        return self


class PPAReject(BaseModel):
    reason: str


class PPAOut(BaseModel):
    id: str
    eid: str
    name: Optional[str]
    from_period: str
    to_period: str
    hours: int
    reason: Optional[str]
    status: str
    rejection_reason: Optional[str]
    date: Optional[str]
