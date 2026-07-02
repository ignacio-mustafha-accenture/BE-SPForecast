import json
from typing import Optional
from loguru import logger
import app.db as db


async def log(
    user_id: Optional[int],
    user_email: Optional[str],
    action: Optional[str],
    method: str,
    endpoint: str,
    request_body,
    response_status: int,
    success: bool,
    error_message: Optional[str],
    ip_address: Optional[str],
    duration_ms: int,
):
    try:
        async with db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit_log (
                    user_id, user_email, action, method, endpoint,
                    request_body, response_status, success, error_message,
                    ip_address, duration_ms
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                user_id,
                user_email,
                action,
                method,
                endpoint,
                json.dumps(request_body) if request_body is not None else None,
                response_status,
                success,
                error_message,
                ip_address,
                duration_ms,
            )
    except Exception:
        logger.exception("Failed to write audit log")
