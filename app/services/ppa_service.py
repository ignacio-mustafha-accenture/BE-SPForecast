import time
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from loguru import logger
import app.db as db
from app.errors import AppError, ForecastException
from app.models.ppa import PPACreate



def _date_range(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def _distribute(total: Decimal, days: int) -> list[Decimal]:
    """Reparte total en days montos de 2 decimales cuya suma es exactamente total."""
    cents = int((Decimal(total) * 100).to_integral_value(ROUND_HALF_UP))
    base, remainder = divmod(cents, days)
    return [
        Decimal(base + (1 if i < remainder else 0)) / 100
        for i in range(days)
    ]


async def _get_workdays(conn, period_name: str, country: str) -> list[date]:
    """Días hábiles del período (L-V, sin feriados del país)."""
    period = await conn.fetchrow(
        "SELECT start_date, end_date FROM periods WHERE period_name=$1",
        period_name,
    )
    if not period:
        return []

    holidays = await conn.fetch(
        "SELECT date FROM holidays WHERE country=$1 AND date BETWEEN $2 AND $3",
        country, period["start_date"], period["end_date"],
    )
    holiday_set = {h["date"] for h in holidays}

    return [
        d for d in _date_range(period["start_date"], period["end_date"])
        if _is_weekday(d) and d not in holiday_set
    ]


async def _apply_ppa_to_daily_hours(
    conn,
    eid: str,
    from_period: str,
    to_period: str,
    hours: int,
    country: str,
) -> None:
    """
    Distribuye las horas PPA entre los días hábiles del período:
      - from_period: chg_ppa -= hours
      - to_period:   chg_ppa += hours
    El reparto por día suma exactamente hours en cada período.
    """
    for period_name, sign in [(from_period, -1), (to_period, 1)]:
        workdays = await _get_workdays(conn, period_name, country)
        if not workdays:
            logger.warning(f"No workdays found for period {period_name}, skipping PPA distribution")
            continue

        amounts = _distribute(Decimal(hours), len(workdays))

        await conn.executemany(
            """
            INSERT INTO employee_daily_hours (eid, date, sah, chg_hl, chg_sl, chg_ppa, updated_at)
            VALUES ($1, $2, 0, 0, 0, $3, NOW())
            ON CONFLICT (eid, date) DO UPDATE SET
                chg_ppa    = employee_daily_hours.chg_ppa + $3,
                updated_at = NOW()
            """,
            [(eid, d, amount * sign) for d, amount in zip(workdays, amounts)],
        )

    logger.info(
        f"PPA applied to daily hours",
        eid=eid, from_period=from_period, to_period=to_period, hours=hours,
    )


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
            emp = await conn.fetchrow(
                "SELECT eid, COALESCE(country, location) AS country FROM employees WHERE eid=$1",
                body.eid,
            )
            if not emp:
                raise ForecastException(AppError.EMPLOYEE_NOT_FOUND)

            for period_name in (body.from_period, body.to_period):
                period = await conn.fetchrow(
                    "SELECT period_name FROM periods WHERE period_name=$1",
                    period_name,
                )
                if not period:
                    raise ForecastException(
                        AppError.PERIOD_NOT_FOUND,
                        f"Período '{period_name}' no encontrado",
                    )

            await conn.execute(
                """
                INSERT INTO ppa_log (eid, from_period, to_period, hours, reason, created_at, created_by)
                VALUES ($1, $2, $3, $4, $5, NOW(), $6)
                """,
                body.eid, body.from_period, body.to_period,
                body.hours, body.reason or None, created_by or None,
            )

            country = emp["country"] or "AR"
            await _apply_ppa_to_daily_hours(
                conn,
                eid=body.eid,
                from_period=body.from_period,
                to_period=body.to_period,
                hours=body.hours,
                country=country,
            )

    duration = int((time.monotonic() - start) * 1000)
    logger.bind(action="ppa:create", request_id=request_id, duration_ms=duration).info(
        "PPA created", eid=body.eid
    )
    return {"ok": True}