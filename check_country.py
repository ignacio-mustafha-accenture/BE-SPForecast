import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg
from collections import Counter

async def main():
    conn = await asyncpg.connect(host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require')

    print('--- valores de country / location en employees activos ---')
    rows = await conn.fetch("""
        SELECT COALESCE(country, location) AS pais, COUNT(*) n
        FROM employees WHERE active = TRUE
        GROUP BY COALESCE(country, location)
        ORDER BY n DESC
    """)
    for r in rows:
        print(f"  {r['pais']!r}: {r['n']}")

    print()
    print('--- cuantos matchean el filtro AR ---')
    n = await conn.fetchval("""
        SELECT COUNT(*) FROM employees
        WHERE active = TRUE
          AND LOWER(COALESCE(country, location)) = ANY(ARRAY['ar','argentina'])
    """)
    print(f"  {n}")

    await conn.close()

asyncio.run(main())
