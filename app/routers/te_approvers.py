from fastapi import APIRouter, Query
from app.dependencies import require_permission
import app.db as db

router = APIRouter()


@router.get("", dependencies=[require_permission("state:read")])
async def list_te_approvers(q: str | None = Query(None)):
    async with db.pool.acquire() as conn:
        if q:
            rows = await conn.fetch(
                "SELECT name FROM te_approvers WHERE name ILIKE $1 ORDER BY name LIMIT 20",
                f"%{q}%",
            )
        else:
            rows = await conn.fetch(
                "SELECT name FROM te_approvers ORDER BY name LIMIT 20"
            )
    return {"items": [r["name"] for r in rows]}
