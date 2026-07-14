from fastapi import APIRouter, Request, Response
from app.errors import AppError, ForecastException
from app.models.auth import (
    LoginRequest, UserCreate, UserUpdate,
    ForgotPasswordRequest, ResetPasswordRequest,
)
from app.services import auth_service
from app.dependencies import require_permission

router = APIRouter()

COOKIE_MAX_AGE = 28800  # 8 hours


@router.post("/login", status_code=201)
async def login(body: LoginRequest, response: Response, request: Request):
    request.state.action = "Login"
    user = await auth_service.authenticate(body.email, body.password)
    token = auth_service.create_access_token({"sub": str(user["id"])})
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=COOKIE_MAX_AGE,
    )
    return {
        "id": user["id"],
        "email": user["email"],
        "full_name": user["full_name"],
        "role": user["role"],
    }


@router.post("/logout")
async def logout(response: Response, request: Request):
    request.state.action = "Logout"
    response.delete_cookie("access_token", path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    request.state.action = "View profile"
    user = request.state.user
    return {
        "id": user["id"],
        "email": user["email"],
        "full_name": user["full_name"],
        "role": user["role"],
        "eid": user.get("eid"),
    }


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request):
    request.state.action = "Request password reset"
    await auth_service.forgot_password(body.email)
    return {"ok": True}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, request: Request):
    request.state.action = "Reset password"
    await auth_service.reset_password(body.token, body.new_password)
    return {"ok": True}


@router.post("/users", status_code=201, dependencies=[require_permission("admin:users")])
async def create_user(body: UserCreate, request: Request):
    request.state.action = f"Create user: {body.email}"
    user = await auth_service.create_user(body)
    return user


@router.get("/users", dependencies=[require_permission("admin:users")])
async def list_users(request: Request):
    request.state.action = "List users"
    return await auth_service.list_users()


@router.patch("/users/{user_id}", dependencies=[require_permission("admin:users")])
async def update_user(user_id: int, body: UserUpdate, request: Request):
    request.state.action = f"Update user {user_id}"
    return await auth_service.update_user(user_id, body)
