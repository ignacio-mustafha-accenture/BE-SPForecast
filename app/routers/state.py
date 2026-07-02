from fastapi import APIRouter, Query
from app.dependencies import require_permission
from app.services import state_service

router = APIRouter()


@router.get("", dependencies=[require_permission("state:read")])
async def get_state(window_offset: int = Query(default=0)):
    return await state_service.get_state(window_offset)
