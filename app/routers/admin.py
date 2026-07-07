from typing import List, Optional
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from app.dependencies import require_permission
from app.errors import AppError, ForecastException
from app.services import permission_service
import app.db as db

router = APIRouter()


# ---------- Audit log ----------

@router.get("/audit-log", dependencies=[require_permission("admin:audit_log")])
async def get_audit_log(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user_id: Optional[int] = None,
    action: Optional[str] = None,
    success: Optional[bool] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
):
    conditions = ["1=1"]
    params: list = []

    def add(cond, val):
        params.append(val)
        conditions.append(f"{cond}${len(params)}")

    if user_id is not None:
        add("user_id=", user_id)
    if action is not None:
        add("action=", action)
    if success is not None:
        add("success=", success)
    if from_date is not None:
        add("created_at>=", from_date)
    if to_date is not None:
        add("created_at<=", to_date)

    where = " AND ".join(conditions)
    offset = (page - 1) * page_size

    n = len(params)
    async with db.pool.acquire() as conn:
        total_row = await conn.fetchrow(f"SELECT COUNT(*) AS cnt FROM audit_log WHERE {where}", *params)
        rows = await conn.fetch(
            f"SELECT * FROM audit_log WHERE {where} ORDER BY created_at DESC LIMIT ${n+1} OFFSET ${n+2}",
            *params, page_size, offset,
        )

    return {
        "total": total_row["cnt"],
        "page": page,
        "page_size": page_size,
        "items": [dict(r) for r in rows],
    }


# ---------- Permissions catalog ----------

@router.get("/permissions", dependencies=[require_permission("admin:permissions")])
async def list_permissions():
    return await permission_service.list_permissions()


# ---------- Role permissions ----------

@router.get("/roles/{role}/permissions", dependencies=[require_permission("admin:permissions")])
async def get_role_permissions(role: str):
    return await permission_service.get_role_permissions(role)


class RolePermissionItem(BaseModel):
    permission_id: int
    granted: bool


@router.patch("/roles/{role}/permissions", dependencies=[require_permission("admin:permissions")])
async def set_role_permissions(role: str, items: List[RolePermissionItem], request: Request):
    if role == "admin":
        raise ForecastException(AppError.VALIDATION_ERROR, "Cannot modify admin role permissions")
    user = request.state.user
    for item in items:
        await permission_service.set_role_permission(role, item.permission_id, item.granted, user["id"])
    return {"ok": True}


# ---------- User permissions ----------

@router.get("/users/{user_id}/permissions", dependencies=[require_permission("admin:permissions")])
async def get_user_permissions(user_id: int):
    return await permission_service.get_user_permissions(user_id)


class UserPermissionItem(BaseModel):
    permission_id: int
    granted: bool


@router.patch("/users/{user_id}/permissions", dependencies=[require_permission("admin:permissions")])
async def set_user_permissions(user_id: int, items: List[UserPermissionItem], request: Request):
    user = request.state.user
    for item in items:
        await permission_service.set_user_permission(user_id, item.permission_id, item.granted, user["id"])
    return {"ok": True}


@router.delete("/users/{user_id}/permissions/{permission_id}", dependencies=[require_permission("admin:permissions")])
async def delete_user_permission(user_id: int, permission_id: int):
    await permission_service.delete_user_permission(user_id, permission_id)
    return {"ok": True}


# ---------- Client catalog ----------

class ClientCreate(BaseModel):
    name: str


@router.get("/clients", dependencies=[require_permission("state:read")])
async def list_clients():
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT client FROM forecast_update WHERE client IS NOT NULL AND client != '' ORDER BY client"
        )
    return {"clients": [r["client"] for r in rows]}


@router.post("/clients", dependencies=[require_permission("employees:update")])
async def add_client(body: ClientCreate):
    # NOTE: requires a `client_catalog` table to persist pre-defined clients.
    # Until that table exists, this endpoint validates the input and returns OK.
    # The GET endpoint already surfaces all distinct clients from forecast_update.
    name = body.name.strip()
    if not name:
        raise ForecastException(AppError.VALIDATION_ERROR, "Client name cannot be empty")
    return {"ok": True}
