import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg

async def main():
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require'
    )

    # 1. DaysToAvailable: cuantos tienen valor?
    total = await conn.fetchval("SELECT COUNT(*) FROM forecast_update")
    con_dias = await conn.fetchval("SELECT COUNT(*) FROM forecast_update WHERE days_available IS NOT NULL")
    print(f"forecast_update: {total} filas, {con_dias} con days_available")

    # 2. first_available: se usa?
    con_fad = await conn.fetchval("SELECT COUNT(*) FROM forecast_update WHERE first_available IS NOT NULL")
    print(f"forecast_update: {con_fad} con first_available")

    # 3. SAH por pais: que paises hay en calendar?
    rows = await conn.fetch("SELECT DISTINCT country FROM calendar ORDER BY country")
    print(f"paises en calendar: {[r['country'] for r in rows]}")

    # 4. SAH por periodo y pais (muestra Ago-P2)
    rows = await conn.fetch("""
        SELECT c.country, COUNT(*) FILTER (WHERE c.is_working_day) * 8 AS sah
        FROM calendar c
        JOIN periods p ON c.date BETWEEN p.start_date AND p.end_date
        WHERE p.period_name = 'Ago-P2'
        GROUP BY c.country
    """)
    print("SAH Ago-P2 por pais:")
    for r in rows:
        print(f"  {r['country']}: {r['sah']}h")

    await conn.close()

asyncio.run(main())
