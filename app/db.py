import asyncpg
from loguru import logger
from app.config import settings

pool: asyncpg.Pool = None


async def create_pool():
    global pool
    pool = await asyncpg.create_pool(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        min_size=2,
        max_size=10,
    )
    logger.info("Database pool connected", host=settings.DB_HOST, database=settings.DB_NAME)


async def close_pool():
    global pool
    if pool:
        await pool.close()
        logger.info("Database pool closed")
