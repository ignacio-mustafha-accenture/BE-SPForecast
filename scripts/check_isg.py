import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg
from collections import Counter

async def main():
    conn = await asyncpg.connect(host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require')

    print('--- isg_aligned en forecast_update ---')
    for r in await conn.fetch("SELECT isg_aligned, COUNT(*) n FROM forecast_update GROUP BY isg_aligned"):
        print(f"  {r['isg_aligned']!r}: {r['n']}")

    print()
    print('--- ringfenced en employees ---')
    for r in await conn.fetch("SELECT ringfenced, COUNT(*) n FROM employees WHERE active GROUP BY ringfenced"):
        print(f"  {r['ringfenced']!r}: {r['n']}")

    print()
    print('--- clientes que mencionan ISG ---')
    rows = await conn.fetch("""
        WITH latest AS (
            SELECT DISTINCT ON (eid) * FROM forecast_update
            ORDER BY eid, updated_at DESC NULLS LAST
        )
        SELECT e.eid, l.client, l.isg_aligned, e.ringfenced
        FROM employees e JOIN latest l ON l.eid = e.eid
        WHERE e.active AND l.client ILIKE '%ISG%'
        ORDER BY l.client
    """)
    for r in rows:
        print(f"  {r['eid']:26} {str(r['client'])[:30]:30} isg={r['isg_aligned']!r} rf={r['ringfenced']}")
    if not rows:
        print('  ninguno')

    await conn.close()

asyncio.run(main())
