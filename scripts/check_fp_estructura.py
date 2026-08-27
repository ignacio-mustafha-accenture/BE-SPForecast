import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg

async def main():
    conn = await asyncpg.connect(host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require')

    print('--- periodos que ya hay en forecast_periods ---')
    for r in await conn.fetch("""
        SELECT period_name, COUNT(*) n,
               COUNT(*) FILTER (WHERE chg_hl > 0 OR chg_sl > 0) con_horas
        FROM forecast_periods GROUP BY period_name ORDER BY period_name
    """):
        print(f"  {r['period_name']:10} {r['n']:4} filas, {r['con_horas']:4} con horas")

    print()
    print('--- hay indice unico por (eid, period_name)? ---')
    for r in await conn.fetch("""
        SELECT indexname, indexdef FROM pg_indexes
        WHERE tablename = 'forecast_periods'
    """):
        print(f"  {r['indexname']}")
        print(f"     {r['indexdef']}")

    await conn.close()

asyncio.run(main())
