from fastapi import APIRouter
from app.dependencies import require_permission
import app.db as db

router = APIRouter()


@router.get("", dependencies=[require_permission("state:read")])
async def list_periods():
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT period_name, start_date, end_date
            FROM periods
            ORDER BY start_date
            """
        )
    return {
        "items": [
            {
                "period_name": r["period_name"],
                "start_date": r["start_date"].isoformat(),
                "end_date": r["end_date"].isoformat(),
            }
            for r in rows
        ]
    }
