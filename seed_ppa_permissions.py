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
        for action, description, endpoint in [
            ('ppa:approve', 'Approve PPA adjustment', '/api/ppa/{id}/approve'),
            ('ppa:reject',  'Reject PPA adjustment',  '/api/ppa/{id}/reject'),
        ]:
            existing = await conn.fetchval("SELECT id FROM permissions WHERE action=$1", action)
            if existing:
                print(f"SKIP: {action} ya existe (id={existing})")
                perm_id = existing
            else:
                perm_id = await conn.fetchval(
                    "INSERT INTO permissions (action, description, method, endpoint) VALUES ($1, $2, 'POST', $3) RETURNING id",
                    action, description, endpoint
                )
                print(f"OK: {action} creado (id={perm_id})")

            for role in ('admin', 'manager'):
                await conn.execute(
                    "INSERT INTO role_permissions (role, permission_id, granted) VALUES ($1, $2, TRUE) ON CONFLICT (role, permission_id) DO NOTHING",
                    role, perm_id
                )
            print(f"  -> asignado a admin y manager")

    rows = await conn.fetch("SELECT id, action FROM permissions WHERE action LIKE 'ppa%' ORDER BY id")
    print("\nPermisos PPA finales:")
    for r in rows:
        print(f"  {r['id']} | {r['action']}")
    await conn.close()

asyncio.run(main())
