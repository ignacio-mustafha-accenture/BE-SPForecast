import time
from loguru import logger
import app.db as db
from app.config import settings
from app.errors import AppError, ForecastException
from app.models.employees import EmployeeUpdate


async def update(eid: str, body: EmployeeUpdate, request_id: str) -> dict:
    logger.bind(action="employees:update", request_id=request_id).info(
        "Updating employee", eid=eid
    )
    start = time.monotonic()

    async with db.pool.acquire() as conn:
        async with conn.transaction():
            emp = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", eid)
            if not emp:
                raise ForecastException(AppError.EMPLOYEE_NOT_FOUND)

            effective_eid = body.new_eid or eid

            if body.new_eid and body.new_eid != eid:
                taken = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", body.new_eid)
                if taken:
                    raise ForecastException(AppError.EMPLOYEE_EID_TAKEN)

            if body.new_eid or body.name or body.cl is not None:
                await conn.execute(
                    """
                    UPDATE employees SET
                        eid  = COALESCE($1, eid),
                        name = COALESCE($2, name),
                        cl   = COALESCE($3, cl)
                    WHERE eid = $4
                    """,
                    body.new_eid or None, body.name or None, body.cl, eid,
                )
                if body.new_eid and body.new_eid != eid:
                    await conn.execute("UPDATE forecast_update SET eid=$1 WHERE eid=$2", body.new_eid, eid)
                    await conn.execute("UPDATE forecast_periods SET eid=$1 WHERE eid=$2", body.new_eid, eid)
                    await conn.execute("UPDATE tickets SET eid=$1 WHERE eid=$2", body.new_eid, eid)

            await conn.execute(
                """
                INSERT INTO forecast_update (
                    eid, client, offering, roll_on, roll_off,
                    account_manager, notes, next_client, chargeability_pct, updated_at
                ) VALUES ($8,$1,$2,$3::date,$4::date,$5,$6,$7,$9,NOW())
                ON CONFLICT (eid) DO UPDATE SET
                    client            = COALESCE($1, forecast_update.client),
                    offering          = COALESCE($2, forecast_update.offering),
                    roll_on           = COALESCE($3::date, forecast_update.roll_on),
                    roll_off          = COALESCE($4::date, forecast_update.roll_off),
                    account_manager   = COALESCE($5, forecast_update.account_manager),
                    notes             = COALESCE($6, forecast_update.notes),
                    next_client       = COALESCE($7, forecast_update.next_client),
                    chargeability_pct = COALESCE($9, forecast_update.chargeability_pct),
                    updated_at        = NOW()
                """,
                body.client or None,
                body.offering or None,
                body.roll_on or None,
                body.roll_off or None,
                body.account_manager or None,
                body.notes or None,
                body.next_client or None,
                effective_eid,
                body.chargeability_pct,
            )

            if body.roll_on or body.roll_off or body.chargeability_pct is not None:
                periods = await conn.fetch("SELECT period_name FROM periods ORDER BY start_date")
                logger.bind(request_id=request_id).debug(
                    "Recalculating periods after employee update", eid=effective_eid
                )
                for p in periods:
                    await conn.execute(
                        "SELECT recalculate_forecast_period($1,$2)", effective_eid, p["period_name"]
                    )

    duration = int((time.monotonic() - start) * 1000)
    logger.bind(action="employees:update", request_id=request_id, duration_ms=duration).info(
        "Employee updated", eid=effective_eid
    )
    return {"ok": True}
