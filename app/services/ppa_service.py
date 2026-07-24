import time
from loguru import logger
import app.db as db
from app.errors import AppError, ForecastException
from app.models.ppa import PPACreate


async def list_ppa(
    eid: str | None = None,
    from_period: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict:
    conditions: list[str] = []
    params: list = []

    if eid:
        params.append(f"%{eid}%")
        conditions.append(f"p.eid ILIKE ${len(params)}")

    if from_period:
        params.append(from_period)
        conditions.append(f"p.from_period = ${len(params)}")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size
    params.append(page_size)
    limit_idx = len(params)
    params.append(offset)
    offset_idx = len(params)

    async with db.pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT p.id::text AS id, p.eid, e.name,
                   p.from_period AS "from", p.to_period AS "to",
                   p.hours AS hs, p.reason,
                   TO_CHAR(p.created_at,'DD/MM/YY') AS date,
                   COALESCE(e.country, e.location) AS country,
                   COUNT(*) OVER () AS _total
            FROM ppa_log p LEFT JOIN employees e ON p.eid=e.eid
            {where}
            ORDER BY p.created_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """, *params)

    total = int(rows[0]["_total"]) if rows else 0
    pages = -(-total // page_size) if page_size > 0 else 0
    items = [{k: v for k, v in dict(r).items() if k != "_total"} for r in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages}


async def create(body: PPACreate, created_by: str, request_id: str) -> dict:
    logger.bind(action="ppa:create", request_id=request_id).info(
        "Creating PPA", eid=body.eid, from_period=body.from_period, to_period=body.to_period
    )
    start = time.monotonic()

    async with db.pool.acquire() as conn:
        async with conn.transaction():
            emp = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", body.eid)
            if not emp:
                raise ForecastException(AppError.EMPLOYEE_NOT_FOUND)

            fp_src = await conn.fetchrow(
                "SELECT id FROM forecast_periods WHERE eid=$1 AND period_name=$2",
                body.eid, body.from_period,
            )
            if not fp_src:
                raise ForecastException(AppError.PERIOD_NOT_FOUND)

            await conn.execute(
                """
                INSERT INTO ppa_log (eid, from_period, to_period, hours, reason, created_at, created_by)
                VALUES ($1,$2,$3,$4,$5,NOW(),$6)
                """,
                body.eid, body.from_period, body.to_period,
                body.hours, body.reason or None, created_by or None,
            )

            # Recalculate both periods via stored proc so ppa_adj is correctly populated
            for period in (body.from_period, body.to_period):
                try:
                    await conn.execute(
                        "SELECT recalculate_forecast_period($1,$2)", body.eid, period
                    )
                except Exception as e:
                    logger.bind(request_id=request_id).warning(
                        "Recalculate failed after PPA", period=period, error=str(e)
                    )

    duration = int((time.monotonic() - start) * 1000)
    logger.bind(action="ppa:create", request_id=request_id, duration_ms=duration).info(
        "PPA created", eid=body.eid
    )
    return {"ok": True}
