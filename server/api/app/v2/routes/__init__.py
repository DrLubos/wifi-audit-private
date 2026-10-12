"""The /api/v2 endpoints (reads PLAN.md 7.3, writes 7.5), one module per endpoint group."""

from fastapi import APIRouter

from . import aps, auth, events, networks, overview, sensors, settings, spectrum

router = APIRouter(prefix="/api/v2")
for _module in (sensors, overview, events, networks, aps, spectrum, settings, auth):
    router.include_router(_module.router)
