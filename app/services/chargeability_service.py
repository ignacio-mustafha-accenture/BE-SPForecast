import app.db as db
from app.errors import AppError, ForecastException
from app.models.chargeability import ChargeabilityBlockCreate
from loguru import logger


def _serialize_block(row: dict) -> dict:
    return {
        "id": row["id"],
        "eid": row["eid"],
        "period_name": row["period_name"],
        "chargeability_pct": float(row["chargeability_pct"]),
        "scenario_type": row["scenario_type"],
        "start_date": row["start_date"].isoformat() if row["start_date"] else None,
        "end_date": row["end_date"].isoformat() if row["end_date"] else None,
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "effectivization_date": row["effectivization_date"].isoformat() if row.get("effectivization_date") else None,
    }


async def list_blocks(eid: str) -> list:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM chargeability_blocks WHERE eid=$1 ORDER BY start_date",
            eid,
        )
        return [_serialize_block(dict(r)) for r in rows]


async def create_block(eid: str, block: ChargeabilityBlockCreate, created_by: str | None) -> dict:
    if block.end_date < block.start_date:
        raise ForecastException(AppError.VALIDATION_ERROR, "end_date must be >= start_date")

    days_diff = (block.end_date - block.start_date).days
    if days_diff > 14:
        raise ForecastException(AppError.VALIDATION_ERROR, "El bloque no puede superar 14 días")

    if block.scenario_type == "assumption" and not block.effectivization_date:
        raise ForecastException(AppError.VALIDATION_ERROR, "effectivization_date is required for assumption blocks")

    if block.effectivization_date and block.effectivization_date > block.end_date:
        raise ForecastException(AppError.VALIDATION_ERROR, "effectivization_date must be <= end_date")

    async with db.pool.acquire() as conn:
        # Verify employee exists
        emp = await conn.fetchrow("SELECT eid FROM employees WHERE eid=$1", eid)
        if not emp:
            raise ForecastException(AppError.EMPLOYEE_NOT_FOUND)

        async with conn.transaction():
            # Check for overlapping blocks
            overlap = await conn.fetchrow(
                """
                SELECT id FROM chargeability_blocks
                WHERE eid=$1 AND NOT (end_date < $2 OR start_date > $3)
                LIMIT 1
                """,
                eid, block.start_date, block.end_date,
            )
            if overlap:
                raise ForecastException(
                    AppError.VALIDATION_ERROR,
                    "El rango se solapa con un bloque existente para este empleado",
                )

            # Resolve period_name from start_date
            period_row = await conn.fetchrow(
                "SELECT period_name FROM periods WHERE start_date <= $1 AND end_date >= $1 LIMIT 1",
                block.start_date,
            )
            period_name = period_row["period_name"] if period_row else None

            # Insert block
            row = await conn.fetchrow(
                """
                INSERT INTO chargeability_blocks
                    (eid, period_name, chargeability_pct, scenario_type,
                     start_date, end_date, created_by, effectivization_date)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING *
                """,
                eid, period_name, block.chargeability_pct, block.scenario_type,
                block.start_date, block.end_date, created_by,
                block.effectivization_date or None,
            )

            # Recalculate the affected period via stored proc (includes all split fields)
            if period_name:
                try:
                    await conn.execute(
                        "SELECT recalculate_forecast_period($1,$2)", eid, period_name
                    )
                except Exception as e:
                    logger.warning("Recalculate failed after chargeability block create", error=str(e))

            return _serialize_block(dict(row))


async def effectivize_employee(eid: str, period_names: list[str] | None, chargeability_pct: float) -> dict:
    async with db.pool.acquire() as conn:
        # Commit the block update in its own transaction before recalculating.
        # Recalculate must run outside the transaction: if the stored proc raises a
        # PostgreSQL exception, the connection enters ABORTED state and the subsequent
        # COMMIT becomes a ROLLBACK, silently undoing the UPDATE.
        async with conn.transaction():
            if period_names:
                rows = await conn.fetch(
                    """
                    UPDATE chargeability_blocks
                    SET scenario_type = 'effective',
                        effectivization_date = NULL,
                        chargeability_pct = $3
                    WHERE eid = $1 AND scenario_type = 'assumption'
                      AND period_name = ANY($2::text[])
                    RETURNING period_name
                    """,
                    eid, period_names, chargeability_pct,
                )
            else:
                rows = await conn.fetch(
                    """
                    UPDATE chargeability_blocks
                    SET scenario_type = 'effective',
                        effectivization_date = NULL,
                        chargeability_pct = $2
                    WHERE eid = $1 AND scenario_type = 'assumption'
                    RETURNING period_name
                    """,
                    eid, chargeability_pct,
                )

        updated = len(rows)
        affected = {r["period_name"] for r in rows if r["period_name"]}
        logger.info("Effectivize blocks updated", eid=eid, updated=updated, affected=list(affected))

        # Always recalculate ALL requested periods, not just the ones that changed.
        # forecast_periods can be stale from a previous run where the stored proc failed.
        # If a period was already effective (updated=0 for it), we still need to sync fp.
        if period_names:
            periods_to_recalc = set(period_names)
        else:
            # "all" mode — recalculate every period that has a block for this employee
            block_period_rows = await conn.fetch(
                "SELECT DISTINCT period_name FROM chargeability_blocks WHERE eid = $1 AND period_name IS NOT NULL",
                eid,
            )
            periods_to_recalc = {r["period_name"] for r in block_period_rows}

        fp_updated = 0
        for pname in periods_to_recalc:
            # Snapshot chg_pct_sl BEFORE any modifications so we can detect the
            # "no blocks but FP has SL" case after the stored proc may zero it out.
            orig = await conn.fetchrow(
                "SELECT chg_pct_sl FROM forecast_periods WHERE eid=$1 AND period_name=$2",
                eid, pname,
            )
            orig_sl = float(orig["chg_pct_sl"] or 0) if orig else 0.0

            # 1) Try the stored proc (handles PPAs, absences, cascadeadas).
            try:
                await conn.execute(
                    "SELECT recalculate_forecast_period($1,$2)", eid, pname
                )
            except Exception as e:
                logger.warning("Recalculate stored proc failed", eid=eid, period=pname, error=str(e))

            if pname in affected:
                # 2a) Assumption blocks were moved to effective → rebuild chg_pct from blocks.
                fp_row = await conn.fetchrow(
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
                    RETURNING fp.chg_pct_hl, fp.chg_pct_sl
                    """,
                    eid, pname,
                )
            elif orig_sl > 0:
                # 2b) No assumption blocks for this period, but forecast_periods had SL.
                # Move SL → HL directly using the requested chargeability_pct.
                fp_row = await conn.fetchrow(
                    """
                    UPDATE forecast_periods fp
                    SET chg_pct_hl = $3,
                        chg_hl     = ROUND(fp.sah * $3 / 100.0),
                        chg        = ROUND(fp.sah * $3 / 100.0),
                        chg_pct_sl = 0,
                        chg_sl     = 0
                    WHERE fp.eid = $1 AND fp.period_name = $2
                    RETURNING fp.chg_pct_hl, fp.chg_pct_sl
                    """,
                    eid, pname, chargeability_pct,
                )
                logger.info("SL→HL fallback (no blocks)", eid=eid, period=pname,
                            orig_sl=orig_sl, hl=chargeability_pct)
            else:
                fp_row = None

            if fp_row:
                fp_updated += 1
                logger.info("forecast_periods updated", eid=eid, period=pname,
                            chg_pct_hl=float(fp_row["chg_pct_hl"]), chg_pct_sl=float(fp_row["chg_pct_sl"]))
            else:
                logger.info("forecast_periods skipped (no change needed)", eid=eid, period=pname)

        # Refresh assumption projection blocks for periods post roll-off
        fu = await conn.fetchrow(
            "SELECT roll_off, client FROM forecast_update WHERE eid=$1 ORDER BY updated_at DESC NULLS LAST LIMIT 1",
            eid,
        )
        if fu and fu["roll_off"]:
            from app.services.assumption_service import get_assumption_num, upsert_projection_blocks
            num = await get_assumption_num(conn, client_name=fu["client"], is_nj=False, eid=eid)
            await upsert_projection_blocks(
                conn, eid,
                ref_date=fu["roll_off"],
                num=num,
                effectivization_date=None,
                request_id="-",
            )
            proj_periods = await conn.fetch(
                "SELECT period_name FROM periods WHERE end_date > $1 ORDER BY start_date LIMIT 6",
                fu["roll_off"],
            )
            for p in proj_periods:
                try:
                    await conn.execute("SELECT recalculate_forecast_period($1,$2)", eid, p["period_name"])
                except Exception as e:
                    logger.warning("Recalc failed for projection period", eid=eid, period=p["period_name"], error=str(e))

    logger.info("Effectivize complete", eid=eid, updated=updated, fp_updated=fp_updated)
    return {"ok": True, "updated": updated + fp_updated}


async def delete_block(block_id: int, eid: str) -> None:
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "DELETE FROM chargeability_blocks WHERE id=$1 AND eid=$2 RETURNING period_name",
                block_id, eid,
            )
            if not row:
                raise ForecastException(AppError.VALIDATION_ERROR, "Bloque no encontrado")

            period_name = row["period_name"]
            if period_name:
                try:
                    await conn.execute(
                        "SELECT recalculate_forecast_period($1,$2)", eid, period_name
                    )
                except Exception as e:
                    logger.warning("Recalculate failed after chargeability block delete", error=str(e))
