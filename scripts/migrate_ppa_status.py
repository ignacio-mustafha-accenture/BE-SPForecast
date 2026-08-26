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
            ALTER TABLE ppa_log
                ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'approved',
                ADD COLUMN IF NOT EXISTS rejection_reason TEXT,
                ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS resolved_by VARCHAR(255)
        """)
        # PPAs existentes ya fueron aplicados, quedan como approved
        print("Migration OK")
    await conn.close()

asyncio.run(main())
