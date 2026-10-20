import asyncio, sys
sys.path.insert(0, ".")
import asyncpg
from app.config import settings

async def main():
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl="require",
    )

    # Usuarios y roles
    users = await conn.fetch("SELECT id, email, role, is_active FROM users ORDER BY id")
    print("Usuarios:")
    for u in users:
        print(f"  id={u['id']} email={u['email']} role={u['role']} active={u['is_active']}")

    # Permisos por rol
    print("\nPermisos por rol:")
    role_perms = await conn.fetch("SELECT role, action FROM role_permissions ORDER BY role, action")
    by_role = {}
    for r in role_perms:
        by_role.setdefault(r["role"], []).append(r["action"])
    for role, perms in by_role.items():
        print(f"  {role}: {perms}")

    # Permisos individuales
    print("\nPermisos individuales (user_permissions):")
    ups = await conn.fetch("SELECT user_id, action, granted FROM user_permissions ORDER BY user_id, action")
    if ups:
        for r in ups:
            print(f"  user_id={r['user_id']} action={r['action']} granted={r['granted']}")
    else:
        print("  (ninguno)")

    await conn.close()

asyncio.run(main())
