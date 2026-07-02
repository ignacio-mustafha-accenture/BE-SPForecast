from typing import List, Optional
import app.db as db
from app.errors import AppError, ForecastException

VALID_ROLES = {"admin", "manager", "viewer"}


async def check(user_id: int, role: str, action: str) -> bool:
    async with db.pool.acquire() as conn:
        # user-level override
        row = await conn.fetchrow(
            """
            SELECT up.granted FROM user_permissions up
            JOIN permissions p ON up.permission_id = p.id
            WHERE up.user_id=$1 AND p.action=$2
            """,
            user_id,
            action,
        )
        if row is not None:
            return row["granted"]

        # role default
        row = await conn.fetchrow(
            """
            SELECT rp.granted FROM role_permissions rp
            JOIN permissions p ON rp.permission_id = p.id
            WHERE rp.role=$1 AND p.action=$2
            """,
            role,
            action,
        )
        if row is not None:
            return row["granted"]

    return False


async def list_permissions() -> list:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, action, description, method, endpoint FROM permissions ORDER BY id"
        )
    return [dict(r) for r in rows]


async def get_role_permissions(role: str) -> list:
    if role not in VALID_ROLES:
        raise ForecastException(AppError.VALIDATION_ERROR, f"Invalid role: {role}")
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id, p.action, p.description, p.method, p.endpoint,
                   COALESCE(rp.granted, FALSE) AS granted
            FROM permissions p
            LEFT JOIN role_permissions rp ON rp.permission_id=p.id AND rp.role=$1
            ORDER BY p.id
            """,
            role,
        )
    return [dict(r) for r in rows]


async def set_role_permission(role: str, permission_id: int, granted: bool, updated_by: int):
    if role == "admin":
        raise ForecastException(AppError.VALIDATION_ERROR, "Cannot modify admin role permissions")
    async with db.pool.acquire() as conn:
        perm = await conn.fetchrow("SELECT id FROM permissions WHERE id=$1", permission_id)
        if not perm:
            raise ForecastException(AppError.PERMISSION_NOT_FOUND)
        await conn.execute(
            """
            INSERT INTO role_permissions (role, permission_id, granted, updated_by)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (role, permission_id)
            DO UPDATE SET granted=$3, updated_by=$4, updated_at=NOW()
            """,
            role,
            permission_id,
            granted,
            updated_by,
        )


async def get_user_permissions(user_id: int) -> list:
    async with db.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
        if not user:
            raise ForecastException(AppError.USER_NOT_FOUND)
        rows = await conn.fetch(
            """
            SELECT p.id, p.action, p.description, up.granted
            FROM user_permissions up
            JOIN permissions p ON up.permission_id=p.id
            WHERE up.user_id=$1
            ORDER BY p.id
            """,
            user_id,
        )
    return [dict(r) for r in rows]


async def set_user_permission(user_id: int, permission_id: int, granted: bool, updated_by: int):
    async with db.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
        if not user:
            raise ForecastException(AppError.USER_NOT_FOUND)
        perm = await conn.fetchrow("SELECT id FROM permissions WHERE id=$1", permission_id)
        if not perm:
            raise ForecastException(AppError.PERMISSION_NOT_FOUND)
        await conn.execute(
            """
            INSERT INTO user_permissions (user_id, permission_id, granted, updated_by)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (user_id, permission_id)
            DO UPDATE SET granted=$3, updated_by=$4, updated_at=NOW()
            """,
            user_id,
            permission_id,
            granted,
            updated_by,
        )


async def delete_user_permission(user_id: int, permission_id: int):
    async with db.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
        if not user:
            raise ForecastException(AppError.USER_NOT_FOUND)
        perm = await conn.fetchrow("SELECT id FROM permissions WHERE id=$1", permission_id)
        if not perm:
            raise ForecastException(AppError.PERMISSION_NOT_FOUND)
        await conn.execute(
            "DELETE FROM user_permissions WHERE user_id=$1 AND permission_id=$2",
            user_id,
            permission_id,
        )
