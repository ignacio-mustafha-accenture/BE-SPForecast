import asyncio, sys
sys.path.insert(0, '.')
from app.config import settings
import asyncpg

async def main():
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl='require'
    )
    rows = await conn.fetch("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'ppa_log'
        ORDER BY ordinal_position
    """)
    for r in rows:
        print(f"  {r['column_name']:30} {r['data_type']}")
    await conn.close()

asyncio.run(main())
