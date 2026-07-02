from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from app.errors import AppError

SKIP_AUTH = {
    "/health",
    "/api/auth/login",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in SKIP_AUTH:
            return await call_next(request)

        token = request.cookies.get("access_token")
        if not token:
            logger.bind(request_id=getattr(request.state, "request_id", "-")).warning(
                "Token missing", path=request.url.path
            )
            return JSONResponse(
                status_code=AppError.TOKEN_MISSING.status,
                content={"code": AppError.TOKEN_MISSING.code, "detail": AppError.TOKEN_MISSING.detail},
            )

        from app.services.auth_service import verify_and_load_user

        user = await verify_and_load_user(token)
        if not user:
            logger.bind(request_id=getattr(request.state, "request_id", "-")).warning(
                "Token invalid or expired", path=request.url.path
            )
            return JSONResponse(
                status_code=AppError.TOKEN_EXPIRED.status,
                content={"code": AppError.TOKEN_EXPIRED.code, "detail": AppError.TOKEN_EXPIRED.detail},
            )

        if not user["is_active"]:
            logger.bind(request_id=getattr(request.state, "request_id", "-")).warning(
                "Inactive user blocked", email=user["email"]
            )
            return JSONResponse(
                status_code=AppError.USER_INACTIVE.status,
                content={"code": AppError.USER_INACTIVE.code, "detail": AppError.USER_INACTIVE.detail},
            )

        request.state.user = user
        logger.bind(request_id=getattr(request.state, "request_id", "-")).debug(
            "Token valid", user_id=user["id"], email=user["email"]
        )
        return await call_next(request)
