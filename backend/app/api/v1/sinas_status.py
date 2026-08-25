"""Sinas integration status — is the sinas-grounded-research package installed in Sinas,
and at what version? Used by the admin UI to surface drift / missing install.
"""

from __future__ import annotations

import functools
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.auth import CallerIdentity, get_caller
from app.config import get_settings
from app.services.sinas import Management, get_management

router = APIRouter(prefix="/sinas-status", tags=["sinas-status"])

EXPECTED_PACKAGE_NAME = "sinas-grounded-research"
# Names this package shipped under before. An instance installed pre-rename
# still carries the old record, and the registry is keyed by exact name — so
# looking only for the current name reports a working install as missing.
LEGACY_PACKAGE_NAMES = ("sinas-grove",)
# Used only when the bundled yaml cannot be read (see _expected_version).
FALLBACK_PACKAGE_VERSION = "0.1.41"


def _bundled_package_path() -> Path | None:
    """Locate the bundled sinas-grounded-research.yaml.

    In the container the Dockerfile copies `package/` next to `backend/` at
    /app/package/. In local dev the repo layout puts it two levels above this
    file. Try both.
    """
    candidates = [
        Path("/app/package/sinas-grounded-research.yaml"),
        Path(__file__).resolve().parents[4] / "package" / "sinas-grounded-research.yaml",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


@functools.lru_cache(maxsize=1)
def _expected_version() -> str:
    """The version this build ships, read from the bundled yaml.

    Hand-maintaining a constant here drifted once already (the code said
    0.1.34 against a 0.1.41 package), turning the drift warning into noise.
    The yaml is the single source of truth.
    """
    path = _bundled_package_path()
    if path is None:
        return FALLBACK_PACKAGE_VERSION
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return str(doc.get("package", {}).get("version") or FALLBACK_PACKAGE_VERSION)
    except (yaml.YAMLError, OSError):
        return FALLBACK_PACKAGE_VERSION


class SinasStatusOut(BaseModel):
    sinas_url: str
    # Always the current package name. The pre-rename identifier is a
    # lookup detail; it is not this product's name and never reaches a
    # user-facing surface — `legacy_record` carries the fact instead.
    package_name: str
    expected_version: str
    installed: bool
    installed_version: str | None
    drift: bool
    legacy_record: bool = False
    note: str | None = None


@router.get("", response_model=SinasStatusOut)
async def get_sinas_status(
    caller: CallerIdentity = Depends(get_caller),
    mgmt: Management = Depends(get_management),
) -> SinasStatusOut:
    settings = get_settings()
    expected = _expected_version()
    if caller.sinas_token is None:
        return SinasStatusOut(
            sinas_url=settings.sinas_url,
            package_name=EXPECTED_PACKAGE_NAME,
            expected_version=expected,
            installed=False,
            installed_version=None,
            drift=False,
            note="cannot query Sinas — no token available",
        )

    legacy_record = False
    pkg = await mgmt.get_installed_package(caller.sinas_token, EXPECTED_PACKAGE_NAME)
    for legacy in LEGACY_PACKAGE_NAMES:
        if pkg is not None:
            break
        pkg = await mgmt.get_installed_package(caller.sinas_token, legacy)
        legacy_record = pkg is not None

    if pkg is None:
        return SinasStatusOut(
            sinas_url=settings.sinas_url,
            package_name=EXPECTED_PACKAGE_NAME,
            expected_version=expected,
            installed=False,
            installed_version=None,
            drift=False,
            note="package not installed in Sinas — install via `sinas package install ./package/sinas-grounded-research.yaml`",
        )

    installed_version = pkg.get("version") or pkg.get("package", {}).get("version")
    drift = installed_version != expected
    notes = []
    if legacy_record:
        notes.append(
            "registered under a pre-rename record — reinstalling the package "
            "retires it"
        )
    if drift:
        notes.append("installed version differs from this SGR build")
    return SinasStatusOut(
        sinas_url=settings.sinas_url,
        package_name=EXPECTED_PACKAGE_NAME,
        expected_version=expected,
        installed=True,
        installed_version=installed_version,
        drift=drift,
        legacy_record=legacy_record,
        note="; ".join(notes) or None,
    )


@router.get("/package.yaml")
async def download_bundled_package() -> Response:
    """Return the bundled sinas-grounded-research.yaml that ships with this SGR build."""
    path = _bundled_package_path()
    if path is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "bundled sinas-grounded-research.yaml not found in this build",
        )
    return Response(
        content=path.read_text(encoding="utf-8"),
        media_type="application/x-yaml",
        headers={"Content-Disposition": 'attachment; filename="sinas-grounded-research.yaml"'},
    )
