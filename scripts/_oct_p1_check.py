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

    rows = await conn.fetch("""
        SELECT
            e.offering,
            COUNT(*) as hc,
            SUM(fp.chg_hl)        as chg_hl,
            SUM(fp.chg_sl)        as chg_sl,
            SUM(fp.chg)           as chg,
            SUM(fp.sah)           as sah,
            SUM(fp.absence_hours) as absence_h,
            ROUND(SUM(fp.chg) / NULLIF(SUM(fp.sah - fp.absence_hours), 0) * 100, 1) as chg_pct
        FROM forecast_periods fp
        JOIN employees e ON e.eid = fp.eid
        WHERE fp.period_name = 'Oct-P1'
        GROUP BY e.offering
        ORDER BY e.offering
    """)

    hdr = f"{'Offering':<12} {'HC':>4} {'CHG_HL':>8} {'CHG_SL':>8} {'CHG':>8} {'SAH':>8} {'Abs_h':>6} {'CHG%':>7}"
    sep = "-" * len(hdr)
    print("=== Oct-P1 por offering (DB) ===")
    print(hdr)
    print(sep)
    for r in rows:
        off = r["offering"] or "?"
        print(f"{off:<12} {r['hc']:>4} {r['chg_hl']:>8.0f} {r['chg_sl']:>8.0f} {r['chg']:>8.0f} {r['sah']:>8.0f} {r['absence_h']:>6.0f} {r['chg_pct']:>6.1f}%")

    tot = await conn.fetchrow("""
        SELECT
            COUNT(*) as hc,
            SUM(fp.chg)           as chg,
            SUM(fp.sah)           as sah,
            SUM(fp.absence_hours) as abs_h,
            ROUND(SUM(fp.chg) / NULLIF(SUM(fp.sah - fp.absence_hours), 0) * 100, 1) as chg_pct
        FROM forecast_periods fp
        WHERE fp.period_name = 'Oct-P1'
    """)
    print(sep)
    print(f"{'TOTAL':<12} {tot['hc']:>4} {'':>8} {'':>8} {tot['chg']:>8.0f} {tot['sah']:>8.0f} {tot['abs_h']:>6.0f} {tot['chg_pct']:>6.1f}%")

    await conn.close()

asyncio.run(main())
