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
                    (eid, period_name, chargeability_pct, scenario_type, start_date, end_date, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                eid, period_name, block.chargeability_pct, block.scenario_type,
                block.start_date, block.end_date, created_by,
            )

            # Recalculate the affected period using the block's chargeability_pct
            if period_name:
                try:
                    fp = await conn.fetchrow(
                        "SELECT sah FROM forecast_periods WHERE eid=$1 AND period_name=$2",
                        eid, period_name,
                    )
                    if fp and fp["sah"]:
                        sah = float(fp["sah"])
                        chg = round(sah * block.chargeability_pct / 100, 2)
                        chg_pct = round(block.chargeability_pct)
                        await conn.execute(
                            """
                            INSERT INTO forecast_periods (eid, period_name, chg, sah, chg_pct)
                            VALUES ($1, $2, $3, $4, $5)
                            ON CONFLICT (eid, period_name)
                            DO UPDATE SET chg=$3, chg_pct=$5
                            """,
                            eid, period_name, chg, sah, chg_pct,
                        )
                except Exception as e:
                    logger.warning("Recalculate failed after chargeability block create", error=str(e))

            return _serialize_block(dict(row))


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
