import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg

async def main():
    conn = await asyncpg.connect(host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require')

    rows = await conn.fetch("""
        SELECT eid, period_name, chg_hl, chg_sl, chg_cascadeadas, chg, sah,
               chg_pct_hl, chg_pct_sl
        FROM forecast_periods
        WHERE chg_sl <> 0
        ORDER BY period_name, eid
        LIMIT 15
    """)
    print(f"{'eid':24} {'periodo':9} {'hl':>6} {'sl':>6} {'ppa':>6} {'chg':>6} {'sah':>6} {'pctHL':>7} {'pctSL':>7}")
    print('-' * 84)
    for r in rows:
        print(f"{r['eid'][:24]:24} {r['period_name']:9} {r['chg_hl']:>6} {r['chg_sl']:>6} "
              f"{r['chg_cascadeadas']:>6} {r['chg']:>6} {r['sah']:>6} "
              f"{r['chg_pct_hl']:>7} {r['chg_pct_sl']:>7}")

    n = await conn.fetchval("SELECT COUNT(*) FROM forecast_periods WHERE chg_sl <> 0")
    print(f"\nTotal de filas con chg_sl distinto de cero: {n}")
    await conn.close()

asyncio.run(main())
