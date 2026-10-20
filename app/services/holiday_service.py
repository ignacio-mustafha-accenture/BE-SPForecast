from datetime import date as date_type

from loguru import logger

import app.db as db
from app.errors import AppError, ForecastException


async def list_holidays(country: str) -> list[dict]:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, country, date::text AS date, name FROM holidays WHERE country=$1 ORDER BY date",
            country,
        )
    return [dict(r) for r in rows]


async def create_holiday(country: str, date: str, name: str) -> dict:
    logger.info(f"[holiday_service] create_holiday → country={country!r} date={date!r} name={name!r}")
    try:
        parsed_date = date_type.fromisoformat(date)
    except ValueError as e:
        logger.error(f"[holiday_service] fecha inválida: {date!r} → {e}")
        raise
    logger.info(f"[holiday_service] fecha parseada → {parsed_date}")
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO holidays (country, date, name)
            VALUES ($1, $2, $3)
            ON CONFLICT (country, date) DO NOTHING
            RETURNING id, country, date::text AS date, name
            """,
            country, parsed_date, name,
        )
    logger.info(f"[holiday_service] INSERT result → {dict(row) if row else 'ON CONFLICT DO NOTHING (ya existe)'}")
    return dict(row) if row else {}


async def delete_holiday(holiday_id: int) -> None:
    async with db.pool.acquire() as conn:
        result = await conn.execute("DELETE FROM holidays WHERE id=$1", holiday_id)
    if result == "DELETE 0":
        raise ForecastException(AppError.PERIOD_NOT_FOUND, f"Holiday {holiday_id} not found")
