from fastapi import Depends, Request
from app.errors import AppError, ForecastException


def require_permission(action: str):
    async def _inner(request: Request) -> dict:
        user = request.state.user
        if user["role"] == "admin":
            return user
        from app.services import permission_service
        allowed = await permission_service.check(user["id"], user["role"], action)
        if not allowed:
            raise ForecastException(AppError.PERMISSION_DENIED, f"Required: {action}")
        return user
    return Depends(_inner)
