import time
from loguru import logger
import app.db as db
from app.errors import AppError, ForecastException


async def recalculate_employee(eid: str, request_id: str = "-") -> dict:
    async with db.pool.acquire() as conn:
        emp = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", eid)
        if not emp:
            raise ForecastException(AppError.EMPLOYEE_NOT_FOUND)

        periods = await conn.fetch("SELECT period_name FROM periods ORDER BY start_date")
        logger.bind(request_id=request_id).debug(
            "Recalculate employee", eid=eid, periods=len(periods)
        )
        start = time.monotonic()
        for p in periods:
            await conn.execute("SELECT recalculate_forecast_period($1,$2)", eid, p["period_name"])

        duration = int((time.monotonic() - start) * 1000)
        logger.bind(request_id=request_id, duration_ms=duration).info(
            "Employee recalculated", eid=eid, updated=len(periods)
        )
        return {"ok": True, "eid": eid, "updated": len(periods)}


async def recalculate_period(period_name: str, request_id: str = "-") -> dict:
    async with db.pool.acquire() as conn:
        period = await conn.fetchrow("SELECT period_name FROM periods WHERE period_name=$1", period_name)
        if not period:
            raise ForecastException(AppError.PERIOD_NOT_FOUND)

        employees = await conn.fetch("SELECT eid FROM employees WHERE active=TRUE")
        logger.bind(request_id=request_id).debug(
            "Recalculate period", period=period_name, employees=len(employees)
        )
        start = time.monotonic()
        for e in employees:
            await conn.execute("SELECT recalculate_forecast_period($1,$2)", e["eid"], period_name)

        duration = int((time.monotonic() - start) * 1000)
        logger.bind(request_id=request_id, duration_ms=duration).info(
            "Period recalculated", period=period_name, updated=len(employees)
        )
        return {"ok": True, "period": period_name, "updated": len(employees)}
