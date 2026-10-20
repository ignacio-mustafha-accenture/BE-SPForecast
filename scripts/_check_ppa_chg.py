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

    n = await conn.fetchval("SELECT COUNT(*) FROM ppa_log")
    print(f"ppa_log total rows: {n}")

    if n > 0:
        rows = await conn.fetch("SELECT eid, from_period, to_period, hours FROM ppa_log LIMIT 10")
        for r in rows:
            print(f"  eid={r['eid']}  from={r['from_period']}  to={r['to_period']}  hours={r['hours']}")

    # Empleados donde chg != chg_hl + chg_sl en Oct-P1
    disc = await conn.fetch("""
        SELECT fp.eid, e.offering, fp.chg_hl, fp.chg_sl,
               fp.chg_hl + fp.chg_sl AS sum_hl_sl,
               fp.chg,
               fp.chg - fp.chg_hl - fp.chg_sl AS diff,
               fp.chg_cascadeadas
        FROM forecast_periods fp
        JOIN employees e ON e.eid = fp.eid
        WHERE fp.period_name = 'Oct-P1'
          AND fp.chg != fp.chg_hl + fp.chg_sl
        ORDER BY diff DESC
        LIMIT 20
    """)
    print(f"\nEmpleados con chg != chg_hl+chg_sl en Oct-P1 ({len(disc)} filas):")
    for r in disc:
        eid = r["eid"]
        off = r["offering"]
        print(f"  {eid} ({off}): hl={r['chg_hl']} sl={r['chg_sl']} sum={r['sum_hl_sl']} chg={r['chg']} diff={r['diff']} cascadeadas={r['chg_cascadeadas']}")

    await conn.close()

asyncio.run(main())
