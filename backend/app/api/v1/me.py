"""Identity introspection — frontend uses this to know who the caller is and
whether the auth flow needs a login screen."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import CallerIdentity, get_caller
from app.config import get_settings

router = APIRouter(tags=["auth"])


class MeOut(BaseModel):
    user_id: uuid.UUID
    roles: list[str]
    is_admin: bool
    auth_mode: str


@router.get("/me", response_model=MeOut)
async def me(caller: CallerIdentity = Depends(get_caller)) -> MeOut:
    return MeOut(
        user_id=caller.user_id,
        roles=caller.roles,
        is_admin=caller.is_admin,
        auth_mode=get_settings().sgr_auth_mode,
    )
