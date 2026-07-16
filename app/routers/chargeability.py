from fastapi import APIRouter, Request
from app.dependencies import require_permission
from app.models.chargeability import ChargeabilityBlockCreate
from app.services import chargeability_service

router = APIRouter()


@router.get("/{eid}/chargeability-blocks", dependencies=[require_permission("employees:update")])
async def list_chargeability_blocks(eid: str, request: Request):
    request.state.action = f"List chargeability blocks: {eid}"
    return await chargeability_service.list_blocks(eid)


@router.post(
    "/{eid}/chargeability-blocks",
    status_code=201,
    dependencies=[require_permission("employees:update")],
)
async def create_chargeability_block(eid: str, body: ChargeabilityBlockCreate, request: Request):
    request.state.action = f"Create chargeability block: {eid}"
    user = request.state.user
    created_by = user.get("email") or None
    return await chargeability_service.create_block(eid, body, created_by)


@router.delete(
    "/{eid}/chargeability-blocks/{block_id}",
    status_code=204,
    dependencies=[require_permission("employees:update")],
)
async def delete_chargeability_block(eid: str, block_id: int, request: Request):
    request.state.action = f"Delete chargeability block #{block_id}: {eid}"
    await chargeability_service.delete_block(block_id, eid)
