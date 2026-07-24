from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sinas import SinasClient
from sinas.exceptions import SinasAPIError, SinasAuthError

from app.config import get_settings
from app.services.sinas import get_admin_client, get_sinas_auth


@dataclass
class CallerIdentity:
    """Resolved identity for a request — either from Sinas auth or simplified mode."""

    user_id: uuid.UUID
    roles: list[str] = field(default_factory=list)
    is_admin: bool = False
    # Bearer token Grove uses to call back to Sinas as this caller.
    # In `sinas` mode → the user's session JWT.
    # In `simplified` mode → the configured SINAS_API_KEY.
    sinas_token: str | None = None
    permission_cache: dict[str, bool] = field(default_factory=dict)

    async def has_permission(self, permission: str) -> bool:
        if self.is_admin:
            return True
        if permission in self.permission_cache:
            return self.permission_cache[permission]
        if self.sinas_token is None:
            self.permission_cache[permission] = False
            return False
        client = SinasClient(
            base_url=get_settings().sinas_url, token=self.sinas_token
        )
        try:
            res = await asyncio.to_thread(
                client.auth.check_permissions, [permission], "AND"
            )
        except SinasAPIError:
            self.permission_cache[permission] = False
            return False
        ok = bool(res.get("result", False))
        self.permission_cache[permission] = ok
        return ok


# Lazily-resolved admin identity for simplified mode (cached for process lifetime).
_simplified_identity: CallerIdentity | None = None
_simplified_lock = asyncio.Lock()


async def _resolve_simplified_identity() -> CallerIdentity:
    global _simplified_identity
    if _simplified_identity is not None:
        return _simplified_identity
    async with _simplified_lock:
        if _simplified_identity is not None:
            return _simplified_identity
        settings = get_settings()
        if not settings.sinas_api_key:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "GROVE_AUTH_MODE=simplified requires SINAS_API_KEY",
            )
        try:
            me = await asyncio.to_thread(get_admin_client().auth.get_me)
        except SinasAuthError as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"SINAS_API_KEY rejected by Sinas /auth/me: {exc}",
            ) from exc
        try:
            user_id = uuid.UUID(me["id"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Sinas /auth/me returned no user id",
            ) from exc
        _simplified_identity = CallerIdentity(
            user_id=user_id,
            roles=list(me.get("roles") or []),
            is_admin=True,  # operator chose to expose admin via the api key
            sinas_token=settings.sinas_api_key,
        )
        return _simplified_identity


# Tokens already validated against Sinas in simplified mode (bounded cache;
# accepts-for-process-lifetime is a deliberate dev-mode laxity).
_seen_valid_tokens: set[str] = set()


async def get_caller(
    authorization: Annotated[str | None, Header()] = None,
) -> CallerIdentity:
    settings = get_settings()
    if settings.grove_auth_mode == "simplified":
        # Simplified mode maps every caller to the admin identity, but a
        # PRESENTED bearer must still be real: the configured api key, or a
        # token Sinas recognises. Accepting arbitrary tokens hides client
        # misconfiguration and turns the mode into no-auth-at-all.
        if authorization:
            if not authorization.lower().startswith("bearer "):
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "malformed authorization header"
                )
            token = authorization.split(" ", 1)[1].strip()
            if token != (settings.sinas_api_key or "") and token not in _seen_valid_tokens:
                client = SinasClient(base_url=settings.sinas_url, token=token)
                try:
                    await asyncio.to_thread(client.auth.get_me)
                except SinasAuthError as exc:
                    raise HTTPException(
                        status.HTTP_401_UNAUTHORIZED, "invalid token"
                    ) from exc
                except SinasAPIError as exc:
                    raise HTTPException(
                        status.HTTP_502_BAD_GATEWAY, f"sinas: {exc}"
                    ) from exc
                if len(_seen_valid_tokens) > 256:
                    _seen_valid_tokens.clear()
                _seen_valid_tokens.add(token)
        return await _resolve_simplified_identity()

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()

    client = SinasClient(base_url=settings.sinas_url, token=token)
    try:
        me = await asyncio.to_thread(client.auth.get_me)
    except SinasAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc
    except SinasAPIError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"sinas: {exc}") from exc

    try:
        user_id = uuid.UUID(me["id"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "sinas /auth/me returned no user id"
        ) from exc
    return CallerIdentity(
        user_id=user_id,
        roles=list(me.get("roles") or []),
        is_admin=False,
        sinas_token=token,
    )


def require_permission(permission: str):
    """FastAPI dependency: ensures the caller holds the given Grove permission."""

    async def _checker(
        caller: CallerIdentity = Depends(get_caller),
    ) -> CallerIdentity:
        if not await caller.has_permission(permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"missing permission: {permission}"
            )
        return caller

    return _checker


# Re-export so FastAPI can mount the SDK's auth router from one place.
__all__ = ["CallerIdentity", "get_caller", "require_permission", "get_sinas_auth"]
