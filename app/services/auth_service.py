import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
from jose import JWTError, jwt
from loguru import logger
from passlib.context import CryptContext

import app.db as db
from app.config import settings
from app.errors import AppError, ForecastException
from app.models.auth import UserCreate, UserUpdate

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _hash_password(password: str) -> str:
    return pwd_context.hash(password)


def _verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm="HS256")


async def verify_and_load_user(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        user_id: int = payload.get("sub")
        if user_id is None:
            return None
    except JWTError:
        return None

    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, full_name, role, is_active, eid FROM users WHERE id=$1",
            int(user_id),
        )
    if not row:
        return None
    return dict(row)


async def authenticate(email: str, password: str) -> dict:
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, full_name, role, is_active, eid, hashed_password FROM users WHERE email=$1",
            email,
        )
    if not row or not _verify_password(password, row["hashed_password"]):
        raise ForecastException(AppError.INVALID_CREDENTIALS)
    if not row["is_active"]:
        raise ForecastException(AppError.USER_INACTIVE)
    return dict(row)


async def create_user(body: UserCreate) -> dict:
    async with db.pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM users WHERE email=$1", body.email)
        if existing:
            raise ForecastException(AppError.USER_ALREADY_EXISTS)
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO users (email, hashed_password, full_name, role, eid)
                VALUES ($1,$2,$3,$4,$5)
                RETURNING id, email, full_name, role, is_active, eid
                """,
                body.email,
                _hash_password(body.password),
                body.full_name,
                body.role,
                body.eid,
            )
        except asyncpg.UniqueViolationError:
            raise ForecastException(AppError.USER_ALREADY_EXISTS)
    return dict(row)


async def list_users() -> list:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, email, full_name, role, is_active, eid FROM users ORDER BY id"
        )
    return [dict(r) for r in rows]


async def get_user(user_id: int) -> dict:
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, full_name, role, is_active, eid FROM users WHERE id=$1",
            user_id,
        )
    if not row:
        raise ForecastException(AppError.USER_NOT_FOUND)
    return dict(row)


async def update_user(user_id: int, body: UserUpdate) -> dict:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise ForecastException(AppError.VALIDATION_ERROR, "No valid fields provided")

    async with db.pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
        if not existing:
            raise ForecastException(AppError.USER_NOT_FOUND)

        cols = list(updates.keys())
        vals = list(updates.values())
        set_clause = ", ".join(f"{c}=${i+1}" for i, c in enumerate(cols))
        vals.append(user_id)

        row = await conn.fetchrow(
            f"UPDATE users SET {set_clause}, updated_at=NOW() WHERE id=${len(vals)}"
            " RETURNING id, email, full_name, role, is_active, eid",
            *vals,
        )
    return dict(row)


async def forgot_password(email: str):
    async with db.pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, email, full_name FROM users WHERE email=$1", email)
        if not user:
            return  # always 200

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=settings.RESET_TOKEN_EXPIRE_MINUTES
        )
        await conn.execute(
            "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES ($1,$2,$3)",
            user["id"],
            token,
            expires_at,
        )

    try:
        from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType

        conf = ConnectionConfig(
            MAIL_USERNAME=settings.MAIL_USERNAME,
            MAIL_PASSWORD=settings.MAIL_PASSWORD,
            MAIL_FROM=settings.MAIL_FROM,
            MAIL_PORT=settings.MAIL_PORT,
            MAIL_SERVER=settings.MAIL_SERVER,
            MAIL_STARTTLS=True,
            MAIL_SSL_TLS=False,
            USE_CREDENTIALS=True,
        )
        message = MessageSchema(
            subject="ForecastOS — Password Reset",
            recipients=[email],
            body=(
                f"Hi {user['full_name'] or email},\n\n"
                f"Use this token to reset your password:\n\n{token}\n\n"
                "This token expires in 30 minutes."
            ),
            subtype=MessageType.plain,
        )
        fm = FastMail(conf)
        await fm.send_message(message)
    except Exception:
        logger.exception("Failed to send password reset email", email=email)


async def reset_password(token: str, new_password: str):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, expires_at, used
            FROM password_reset_tokens
            WHERE token=$1
            """,
            token,
        )
        if not row:
            raise ForecastException(AppError.RESET_TOKEN_INVALID)
        if row["used"]:
            raise ForecastException(AppError.RESET_TOKEN_INVALID)
        expires = row["expires_at"]
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            raise ForecastException(AppError.RESET_TOKEN_INVALID)

        hashed = _hash_password(new_password)
        await conn.execute(
            "UPDATE users SET hashed_password=$1, updated_at=NOW() WHERE id=$2",
            hashed,
            row["user_id"],
        )
        await conn.execute(
            "UPDATE password_reset_tokens SET used=TRUE WHERE id=$1",
            row["id"],
        )
