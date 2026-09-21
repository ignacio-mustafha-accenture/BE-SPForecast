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
            ALTER TABLE tickets
              ADD COLUMN IF NOT EXISTS hours_chargeable INTEGER,
              ADD COLUMN IF NOT EXISTS hours_standard   INTEGER
        """)
        print("OK — hours_chargeable y hours_standard agregadas a tickets")
    await conn.close()
    print("Migration completada.")

asyncio.run(main())
