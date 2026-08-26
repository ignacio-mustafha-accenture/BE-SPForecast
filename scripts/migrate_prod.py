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
    await conn.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS offering VARCHAR(100)")
    print("OK: migration applied")
    await conn.close()

asyncio.run(main())
