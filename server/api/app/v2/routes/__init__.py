"""The /api/v2 read endpoints (PLAN.md 7.3), one module per endpoint group."""

from fastapi import APIRouter

from . import aps, events, networks, overview, sensors, settings, spectrum

router = APIRouter(prefix="/api/v2")
for _module in (sensors, overview, events, networks, aps, spectrum, settings):
    router.include_router(_module.router)
