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
            pname = p["period_name"]
            try:
                await conn.execute("SELECT recalculate_forecast_period($1,$2)", eid, pname)
            except Exception as e:
                logger.warning("Stored proc failed during recalculate", eid=eid, period=pname, error=str(e))

            # Direct UPDATE from chargeability_blocks as fallback/override.
            # Ensures chg_pct_hl and chg_pct_sl always match actual block state.
            # Uses fp.sah (calendar value written by stored proc, without sah_ppa_adj)
            # so that SAH PPA adjustments don't bleed into CHG computation.
            await conn.execute(
                """
                WITH totals AS (
                    SELECT
                        COALESCE(SUM(chargeability_pct) FILTER (WHERE scenario_type = 'effective'),  0) AS hl_pct,
                        COALESCE(SUM(chargeability_pct) FILTER (WHERE scenario_type = 'assumption'), 0) AS sl_pct
                    FROM chargeability_blocks
                    WHERE eid = $1 AND period_name = $2
                )
                UPDATE forecast_periods fp
                SET chg_pct_hl = t.hl_pct,
                    chg_pct_sl = t.sl_pct,
                    chg_hl     = ROUND(fp.sah * t.hl_pct / 100.0),
                    chg_sl     = ROUND(fp.sah * t.sl_pct / 100.0),
                    chg        = ROUND(fp.sah * (t.hl_pct + t.sl_pct) / 100.0)
                FROM totals t
                WHERE fp.eid = $1 AND fp.period_name = $2
                """,
                eid, pname,
            )

            # Re-apply SAH PPA adjustment (stored proc overwrites sah from calendar).
            # This runs AFTER the fallback so chg_hl uses calendar sah, not adjusted sah.
            await conn.execute(
                """
                UPDATE forecast_periods fp
                SET sah        = fp.sah + COALESCE(fp.sah_ppa_adj, 0),
                    chg_pct    = CASE WHEN fp.sah + COALESCE(fp.sah_ppa_adj, 0) > 0
                                      THEN ROUND(fp.chg / (fp.sah + COALESCE(fp.sah_ppa_adj, 0)) * 100, 2)
                                      ELSE 0 END,
                    chg_pct_hl = CASE WHEN fp.sah + COALESCE(fp.sah_ppa_adj, 0) > 0
                                      THEN ROUND((fp.chg_hl + COALESCE(fp.chg_cascadeadas_hl, 0))
                                                 / (fp.sah + COALESCE(fp.sah_ppa_adj, 0)) * 100, 2)
                                      ELSE 0 END,
                    chg_pct_sl = CASE WHEN fp.sah + COALESCE(fp.sah_ppa_adj, 0) > 0
                                      THEN ROUND(fp.chg_sl / (fp.sah + COALESCE(fp.sah_ppa_adj, 0)) * 100, 2)
                                      ELSE 0 END
                WHERE fp.eid = $1 AND fp.period_name = $2 AND COALESCE(fp.sah_ppa_adj, 0) != 0
                """,
                eid, pname,
            )

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
            await conn.execute(
                """
                UPDATE forecast_periods fp
                SET sah        = fp.sah + COALESCE(fp.sah_ppa_adj, 0),
                    chg_pct    = CASE WHEN fp.sah + COALESCE(fp.sah_ppa_adj, 0) > 0
                                      THEN ROUND(fp.chg / (fp.sah + COALESCE(fp.sah_ppa_adj, 0)) * 100, 2)
                                      ELSE 0 END,
                    chg_pct_hl = CASE WHEN fp.sah + COALESCE(fp.sah_ppa_adj, 0) > 0
                                      THEN ROUND((fp.chg_hl + COALESCE(fp.chg_cascadeadas_hl, 0))
                                                 / (fp.sah + COALESCE(fp.sah_ppa_adj, 0)) * 100, 2)
                                      ELSE 0 END,
                    chg_pct_sl = CASE WHEN fp.sah + COALESCE(fp.sah_ppa_adj, 0) > 0
                                      THEN ROUND(fp.chg_sl / (fp.sah + COALESCE(fp.sah_ppa_adj, 0)) * 100, 2)
                                      ELSE 0 END
                WHERE fp.eid = $1 AND fp.period_name = $2 AND COALESCE(fp.sah_ppa_adj, 0) != 0
                """,
                e["eid"], period_name,
            )

        duration = int((time.monotonic() - start) * 1000)
        logger.bind(request_id=request_id, duration_ms=duration).info(
            "Period recalculated", period=period_name, updated=len(employees)
        )
        return {"ok": True, "period": period_name, "updated": len(employees)}
