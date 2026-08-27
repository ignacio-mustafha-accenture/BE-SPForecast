import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg

async def main():
    conn = await asyncpg.connect(host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require')
    r = await conn.fetchrow("""
        WITH latest AS (SELECT DISTINCT ON (eid) * FROM forecast_update
                        ORDER BY eid, updated_at DESC NULLS LAST)
        SELECT e.eid, e.name, l.client, l.offering, l.roll_on, l.roll_off
        FROM employees e LEFT JOIN latest l ON l.eid = e.eid
        WHERE e.eid = 'maria.jose.matar'
    """)
    print(dict(r))
    await conn.close()

asyncio.run(main())
