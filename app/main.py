from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import create_pool, close_pool
from app.errors import AppError, ForecastException
from app.logger import setup_logging
from app.middleware.audit import AuditMiddleware
from app.middleware.auth import AuthMiddleware
from app.middleware.request_id import RequestIDMiddleware
from app.routers import auth, state, tickets, employees, ppa, recalculate, sync, admin, chargeability
from loguru import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    logger.info(
        "ForecastOS starting",
        log_level=settings.LOG_LEVEL,
        log_format=settings.LOG_FORMAT,
    )
    await create_pool()
    yield
    await close_pool()


app = FastAPI(title="ForecastOS API", lifespan=lifespan)


# --- Exception handlers ---

@app.exception_handler(ForecastException)
async def forecast_handler(request: Request, exc: ForecastException):
    return JSONResponse(
        status_code=exc.error.status,
        content={"code": exc.error.code, "detail": exc.extra or exc.error.detail},
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"code": AppError.VALIDATION_ERROR.code, "detail": exc.errors()},
    )


@app.exception_handler(Exception)
async def generic_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error")
    return JSONResponse(
        status_code=500,
        content={"code": AppError.INTERNAL_ERROR.code, "detail": AppError.INTERNAL_ERROR.detail},
    )


# --- Middleware (last added = first executed) ---

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.CORS_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuditMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(RequestIDMiddleware)


# --- Health ---

@app.get("/health")
async def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


# --- Routers ---

app.include_router(auth.router,         prefix="/api/auth",         tags=["auth"])
app.include_router(state.router,        prefix="/api/state",        tags=["state"])
app.include_router(tickets.router,      prefix="/api/tickets",      tags=["tickets"])
app.include_router(employees.router,    prefix="/api/employees",    tags=["employees"])
app.include_router(ppa.router,          prefix="/api/ppa",          tags=["ppa"])
app.include_router(recalculate.router,  prefix="/api/recalculate",  tags=["recalculate"])
app.include_router(sync.router,         prefix="/api/sync",         tags=["sync"])
app.include_router(admin.router,        prefix="/api/admin",        tags=["admin"])
app.include_router(chargeability.router, prefix="/api/employees",    tags=["chargeability"])
