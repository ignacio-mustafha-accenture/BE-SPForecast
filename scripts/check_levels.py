import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg

async def main():
    conn = await asyncpg.connect(host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require')

    print('--- niveles (CL) de empleados activos ---')
    for r in await conn.fetch("""
        SELECT cl, COUNT(*) n FROM employees
        WHERE active = TRUE GROUP BY cl ORDER BY cl
    """):
        print(f"  CL {r['cl']}: {r['n']}")

    print()
    print('--- el endpoint de employees filtra por cl? ---')
    await conn.close()

asyncio.run(main())
