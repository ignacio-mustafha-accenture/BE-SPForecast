import asyncio, asyncpg
from app.config import settings

async def main():
    conn = await asyncpg.connect(host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl="require")
    await conn.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS reserva_status VARCHAR(40)")
    print("columna reserva_status agregada")
    await conn.close()

asyncio.run(main())
