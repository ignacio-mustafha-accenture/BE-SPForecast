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

        # Recalculate outside the transaction — same pattern as recalculate_service.py
        for pname in affected:
            try:
                await conn.execute(
                    "SELECT recalculate_forecast_period($1,$2)", eid, pname
                )
            except Exception as e:
                logger.warning("Recalculate failed after effectivize", error=str(e))

    return {"ok": True, "updated": updated}


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
