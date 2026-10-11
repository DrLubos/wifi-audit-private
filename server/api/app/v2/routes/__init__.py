"""The /api/v2 read endpoints (PLAN.md 7.3), one module per endpoint group."""

from fastapi import APIRouter

from . import events, networks, overview, sensors

router = APIRouter(prefix="/api/v2")
for _module in (sensors, overview, events, networks):
    router.include_router(_module.router)
