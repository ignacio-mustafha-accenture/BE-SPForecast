import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg

async def main():
    conn = await asyncpg.connect(host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require')

    print('--- forecast_periods (lo que vino del CSV) ---')
    for r in await conn.fetch("""
        SELECT period_name, chg_hl, chg_sl, sah, chg_pct_hl
        FROM forecast_periods WHERE eid='maria.jose.matar'
        AND period_name IN ('Ago-P2','Sep-P1')
    """):
        print(f"  {r['period_name']:8} hl={r['chg_hl']} sl={r['chg_sl']} sah={r['sah']} pct={r['chg_pct_hl']}")

    print()
    print('--- employee_daily_hours en Ago-P2 ---')
    rows = await conn.fetch("""
        SELECT date, sah, chg_hl, chg_sl, chg_ppa
        FROM employee_daily_hours
        WHERE eid='maria.jose.matar' AND date BETWEEN '2026-08-16' AND '2026-08-31'
        ORDER BY date
    """)
    print(f"  {len(rows)} dias cargados")
    for r in rows[:5]:
        print(f"    {r['date']} sah={r['sah']} hl={r['chg_hl']} sl={r['chg_sl']}")

    await conn.close()

asyncio.run(main())
