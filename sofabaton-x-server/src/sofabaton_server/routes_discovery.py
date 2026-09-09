"""``/api/v1/discovery``: hubs seen on the LAN and on-demand scans (plan S4)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from . import API_PREFIX
from .discovery import DiscoveryService, SeenHub

router = APIRouter(prefix=f"{API_PREFIX}/discovery", tags=["discovery"])


class ScanRequest(BaseModel):
    timeout: float = Field(5.0, gt=0, le=60, description="seconds to listen for advertisements")


def _discovery(request: Request) -> DiscoveryService:
    return request.app.state.discovery


@router.get("/hubs", operation_id="listDiscoveredHubs", response_model=list[SeenHub],
            summary="Hubs advertised on the LAN (live table; our own proxies excluded)")
async def list_discovered(request: Request) -> list[SeenHub]:
    return _discovery(request).seen()


@router.post("/scan", operation_id="scanForHubs", response_model=list[SeenHub],
             summary="Listen for hub advertisements for a while, then return the table")
async def scan(request: Request, body: ScanRequest | None = None) -> list[SeenHub]:
    timeout = body.timeout if body is not None else 5.0
    return await _discovery(request).scan(timeout)
