import time
import json
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

STRIP_FIELDS = {"password", "new_password", "hashed_password", "token"}

TRACKED_ACTION_PREFIXES = (
    "Create ticket:",
    "Approve ticket #",
    "Reject ticket #",
    "Create PPA:",
)


def _strip_sensitive(body: dict) -> dict:
    return {k: ("***" if k in STRIP_FIELDS else v) for k, v in body.items()}


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()

        body_bytes = await request.body()
        request_body = None
        if body_bytes:
            try:
                raw = json.loads(body_bytes)
                request_body = _strip_sensitive(raw) if isinstance(raw, dict) else raw
            except Exception:
                request_body = None

        async def receive():
            return {"type": "http.request", "body": body_bytes}

        request._receive = receive

        response = await call_next(request)
        duration_ms = int((time.monotonic() - start) * 1000)

        user = getattr(request.state, "user", None)
        request_id = getattr(request.state, "request_id", None)

        logger.bind(
            request_id=request_id,
            user_id=user["id"] if user else None,
            user_email=user["email"] if user else None,
            duration_ms=duration_ms,
        ).info("Response sent", status=response.status_code, path=request.url.path)

        action = getattr(request.state, "action", None)
        if not action or not any(action.startswith(p) for p in TRACKED_ACTION_PREFIXES):
            return response

        from app.services.audit_service import log as audit_log

        await audit_log(
            user_id=user["id"] if user else None,
            user_email=user["email"] if user else None,
            action=action,
            method=request.method,
            endpoint=str(request.url.path),
            request_body=request_body,
            response_status=response.status_code,
            success=response.status_code < 400,
            error_message=None,
            ip_address=request.client.host if request.client else None,
            duration_ms=duration_ms,
        )

        return response
