from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, field_validator


class ChargeabilityBlockCreate(BaseModel):
    start_date: date
    end_date: date
    chargeability_pct: float
    scenario_type: str = "assumption"

    @field_validator("chargeability_pct")
    @classmethod
    def pct_range(cls, v: float) -> float:
        if not (0 <= v <= 100):
            raise ValueError("chargeability_pct must be between 0 and 100")
        return v

    @field_validator("scenario_type")
    @classmethod
    def valid_scenario(cls, v: str) -> str:
        if v not in ("assumption", "effective"):
            raise ValueError("scenario_type must be 'assumption' or 'effective'")
        return v


class ChargeabilityBlockResponse(BaseModel):
    id: int
    eid: str
    period_name: Optional[str] = None
    chargeability_pct: float
    scenario_type: str
    start_date: str
    end_date: str
    created_by: Optional[str] = None
    created_at: str
