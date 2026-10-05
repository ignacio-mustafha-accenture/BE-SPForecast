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
    async with conn.transaction():
        await conn.execute("""
            ALTER TABLE employee_daily_hours
              ADD COLUMN IF NOT EXISTS chg_ppa_sl NUMERIC DEFAULT 0
        """)
        print("OK — chg_ppa_sl agregada a employee_daily_hours")
    await conn.close()
    print("Migration completada.")

asyncio.run(main())
