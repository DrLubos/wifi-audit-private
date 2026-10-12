"""POST /api/v2/auth/check - the Unlock button of the write-token box: 204
with the right token, 401 without it, 403 when writes are disabled
(PLAN.md 7.5, deps.require_write)."""

from fastapi import APIRouter, Depends, Response

from .. import deps

router = APIRouter(prefix="/auth", tags=["v2 auth"])


@router.post("/check", status_code=204, dependencies=[Depends(deps.require_write)])
def check():
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
