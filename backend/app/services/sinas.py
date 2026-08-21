"""Sinas integration — wraps the official `sinas` Python SDK.

Provides:
  - `sinas_auth` singleton — FastAPI dep that validates bearer tokens and
    returns an authenticated `SinasClient` per request. Its `.router`
    auto-exposes /login, /verify-otp, /refresh, /logout, /me.
  - `get_admin_client()` — `SinasClient` configured with SINAS_API_KEY,
    used in simplified-auth mode for all Sinas callbacks.
  - `Management` — thin async client for management operations the SDK
    doesn't expose (skills CRUD, package status). Token-per-call.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sinas import SinasClient
from sinas.integrations.fastapi import SinasAuth

from app.config import get_settings

log = logging.getLogger(__name__)


# ────────────────────── singletons ──────────────────────
_sinas_auth: SinasAuth | None = None
_admin_client: SinasClient | None = None


def get_sinas_auth() -> SinasAuth:
    global _sinas_auth
    if _sinas_auth is None:
        _sinas_auth = SinasAuth(base_url=get_settings().sinas_url, auto_error=False)
    return _sinas_auth


def get_admin_client() -> SinasClient:
    """Returns the SinasClient used in simplified-auth mode."""
    global _admin_client
    if _admin_client is None:
        settings = get_settings()
        if not settings.sinas_api_key:
            raise RuntimeError(
                "SGR_AUTH_MODE=simplified requires SINAS_API_KEY to be set"
            )
        _admin_client = SinasClient(
            base_url=settings.sinas_url, api_key=settings.sinas_api_key
        )
    return _admin_client


# ────────────────────── management API ──────────────────────
class Management:
    """Async wrapper for Sinas management endpoints not exposed by the SDK
    (skills, packages). Token is supplied per-call so the same instance is
    safe to share across requests."""

    def __init__(self, base_url: str):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(30.0, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    # ── skills ──
    async def list_skills(self, token: str, namespace: str) -> list[dict[str, Any]]:
        resp = await self._client.get(
            "/api/v1/skills", params={"namespace": namespace}, headers=self._bearer(token)
        )
        resp.raise_for_status()
        body = resp.json()
        return body if isinstance(body, list) else body.get("skills", [])

    async def get_skill(
        self, token: str, namespace: str, name: str
    ) -> dict[str, Any] | None:
        resp = await self._client.get(
            f"/api/v1/skills/{namespace}/{name}", headers=self._bearer(token)
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def upsert_skill(
        self,
        token: str,
        namespace: str,
        name: str,
        description: str,
        content: str,
    ) -> dict[str, Any]:
        resp = await self._client.put(
            f"/api/v1/skills/{namespace}/{name}",
            json={
                "namespace": namespace,
                "name": name,
                "description": description,
                "content": content,
                "preload": False,
            },
            headers=self._bearer(token),
        )
        resp.raise_for_status()
        return resp.json()

    async def delete_skill(self, token: str, namespace: str, name: str) -> None:
        resp = await self._client.delete(
            f"/api/v1/skills/{namespace}/{name}", headers=self._bearer(token)
        )
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()

    # ── packages ──
    async def get_installed_package(
        self, token: str, name: str
    ) -> dict[str, Any] | None:
        resp = await self._client.get(
            f"/api/v1/packages/{name}", headers=self._bearer(token)
        )
        if resp.status_code in (403, 404):
            return None
        resp.raise_for_status()
        return resp.json()


_management: Management | None = None


def get_management() -> Management:
    global _management
    if _management is None:
        _management = Management(get_settings().sinas_url)
    return _management
