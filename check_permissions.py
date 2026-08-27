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
    # Ver permisos existentes de PPA
    rows = await conn.fetch("SELECT id, action, description FROM permissions WHERE action LIKE 'ppa%' ORDER BY id")
    print("Permisos PPA actuales:")
    for r in rows:
        print(f"  {r['id']} | {r['action']} | {r['description']}")
    await conn.close()

asyncio.run(main())
