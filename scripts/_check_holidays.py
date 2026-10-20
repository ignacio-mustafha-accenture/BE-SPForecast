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

    # Estructura de la tabla holidays
    cols = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name='holidays' ORDER BY ordinal_position"
    )
    print("Columnas tabla holidays:", [r["column_name"] for r in cols])

    # Feriados por país en holidays
    for code in ("AR", "Argentina", "MX", "Mexico", "CR", "Costa Rica"):
        rows = await conn.fetch(
            "SELECT id, country, date::text, name FROM holidays WHERE country=$1 ORDER BY date", code
        )
        if rows:
            print(f"\nHolidays country='{code}' ({len(rows)} filas):")
            for r in rows:
                print(f"  id={r['id']} date={r['date']} name={r['name']}")

    # Calendar Oct 12
    rows2 = await conn.fetch(
        "SELECT date, country, is_working_day, name FROM calendar WHERE date='2026-10-12'"
    )
    print(f"\nCalendar 2026-10-12:")
    for r in rows2:
        print(f"  country={r['country']} is_working_day={r['is_working_day']} name={r['name']}")

    await conn.close()

asyncio.run(main())
