import asyncio
import os
from datetime import datetime, timezone
from loguru import logger
from fastapi import APIRouter, Request
from app.config import settings
from app.dependencies import require_permission
from app.errors import AppError, ForecastException

router = APIRouter()


@router.post("", dependencies=[require_permission("sync:run")])
async def run_sync(request: Request):
    request_id = request.state.request_id
    script = os.path.abspath(settings.LOAD_DATA_SCRIPT)
    cwd = os.path.abspath(settings.LOAD_DATA_CWD)
    started_at = datetime.now(timezone.utc).isoformat()

    logger.bind(request_id=request_id).info("Sync script started", script=script)

    try:
        proc = await asyncio.create_subprocess_exec(
            "python", script,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            logger.bind(request_id=request_id).error("Sync script timed out")
            raise ForecastException(AppError.INTERNAL_ERROR, "Sync script timed out")

        output = stdout.decode("utf-8", errors="replace")
        if stderr:
            logger.bind(request_id=request_id).warning("Sync stderr", stderr=stderr.decode("utf-8", errors="replace")[:500])

        if proc.returncode != 0:
            logger.bind(request_id=request_id).error("Sync script failed", returncode=proc.returncode)
            raise ForecastException(AppError.INTERNAL_ERROR, "Sync script failed")

        finished_at = datetime.now(timezone.utc).isoformat()
        logger.bind(request_id=request_id).info("Sync script finished")
        return {"ok": True, "started_at": started_at, "finished_at": finished_at, "output": output}

    except ForecastException:
        raise
    except Exception:
        logger.bind(request_id=request_id).exception("Sync unexpected error")
        raise ForecastException(AppError.INTERNAL_ERROR)
