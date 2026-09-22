"""Public instance discovery — proxies upstream Sinas /info.

Frontend hits this before login to know which auth_mode to render
(otp / password / password+otp).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

from app.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["info"])


@router.get("/info")
async def get_info() -> dict[str, Any]:
    settings = get_settings()
    url = settings.sinas_url.rstrip("/") + "/info"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        log.error("Upstream /info failed: %s", e)
        raise HTTPException(status_code=502, detail="Upstream Sinas /info unreachable") from e
