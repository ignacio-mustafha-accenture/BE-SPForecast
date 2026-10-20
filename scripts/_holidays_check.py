import asyncio, sys
sys.path.insert(0, ".")
import asyncpg
from app.config import settings

async def main():
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl="require",
    )

    period = await conn.fetchrow(
        "SELECT start_date, end_date FROM periods WHERE period_name = $1", "Oct-P1"
    )
    sd, ed = period["start_date"], period["end_date"]
    print(f"Oct-P1: {sd} → {ed}")

    # Feriados (días de semana no hábiles) por país
    rows = await conn.fetch("""
        SELECT country,
               COUNT(*) as holidays,
               STRING_AGG(date::text, ', ' ORDER BY date) as fechas
        FROM calendar
        WHERE date BETWEEN $1 AND $2
          AND is_working_day = false
          AND EXTRACT(DOW FROM date) NOT IN (0, 6)
        GROUP BY country ORDER BY country
    """, sd, ed)

    print("\nFeriados (días de semana no hábiles):")
    for r in rows:
        print(f"  {r['country']:15s}: {r['holidays']} feriado(s) → {r['fechas']}")

    # Días hábiles totales por país
    rows2 = await conn.fetch("""
        SELECT country,
               SUM(CASE WHEN is_working_day THEN 1 ELSE 0 END)::int as working_days
        FROM calendar
        WHERE date BETWEEN $1 AND $2
        GROUP BY country ORDER BY country
    """, sd, ed)

    print("\nDías hábiles → SAH:")
    for r in rows2:
        print(f"  {r['country']:15s}: {r['working_days']} días = {r['working_days'] * 8}h SAH")

    await conn.close()

asyncio.run(main())
