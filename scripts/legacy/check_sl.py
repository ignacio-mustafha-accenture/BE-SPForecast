import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg

async def main():
    conn = await asyncpg.connect(host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require')
    rows = await conn.fetch("""
        SELECT eid, chg_hl, chg_sl, chg_cascadeadas, sah, chg_pct_hl, chg_pct_sl
        FROM forecast_periods
        WHERE period_name='Ago-P2' AND (chg_sl <> 0 OR chg_cascadeadas <> 0)
        ORDER BY eid
    """)
    if not rows:
        print('Nadie tiene SL ni PPA en Ago-P2.')
        print('Por eso Neto, HL y SL dan lo mismo para todos.')
    else:
        print(f'{len(rows)} con SL o PPA en Ago-P2:')
        for r in rows:
            neto = round((r['chg_hl'] + r['chg_sl']) / r['sah'] * 100, 2) if r['sah'] else 0
            print(f"  {r['eid']:26} hl={r['chg_hl']:>6} sl={r['chg_sl']:>6} ppa={r['chg_cascadeadas']:>6} "
                  f"| HL={r['chg_pct_hl']}% SL={r['chg_pct_sl']}% Neto={neto}%")
    await conn.close()

asyncio.run(main())
