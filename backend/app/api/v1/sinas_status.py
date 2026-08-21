"""Sinas integration status — is the sinas-grounded-research package installed in Sinas,
and at what version? Used by the admin UI to surface drift / missing install.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.auth import CallerIdentity, get_caller
from app.config import get_settings
from app.services.sinas import Management, get_management

router = APIRouter(prefix="/sinas-status", tags=["sinas-status"])

EXPECTED_PACKAGE_NAME = "sinas-grounded-research"
# Keep in lockstep with package/sinas-grounded-research.yaml — the Activity page
# shows a drift warning when the installed version differs.
EXPECTED_PACKAGE_VERSION = "0.1.34"


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


class SinasStatusOut(BaseModel):
    sinas_url: str
    package_name: str
    expected_version: str
    installed: bool
    installed_version: str | None
    drift: bool
    note: str | None = None


@router.get("", response_model=SinasStatusOut)
async def get_sinas_status(
    caller: CallerIdentity = Depends(get_caller),
    mgmt: Management = Depends(get_management),
) -> SinasStatusOut:
    settings = get_settings()
    if caller.sinas_token is None:
        return SinasStatusOut(
            sinas_url=settings.sinas_url,
            package_name=EXPECTED_PACKAGE_NAME,
            expected_version=EXPECTED_PACKAGE_VERSION,
            installed=False,
            installed_version=None,
            drift=False,
            note="cannot query Sinas — no token available",
        )

    pkg = await mgmt.get_installed_package(caller.sinas_token, EXPECTED_PACKAGE_NAME)
    if pkg is None:
        return SinasStatusOut(
            sinas_url=settings.sinas_url,
            package_name=EXPECTED_PACKAGE_NAME,
            expected_version=EXPECTED_PACKAGE_VERSION,
            installed=False,
            installed_version=None,
            drift=False,
            note="package not installed in Sinas — install via `sinas package install ./package/sinas-grounded-research.yaml`",
        )

    installed_version = pkg.get("version") or pkg.get("package", {}).get("version")
    drift = installed_version != EXPECTED_PACKAGE_VERSION
    return SinasStatusOut(
        sinas_url=settings.sinas_url,
        package_name=EXPECTED_PACKAGE_NAME,
        expected_version=EXPECTED_PACKAGE_VERSION,
        installed=True,
        installed_version=installed_version,
        drift=drift,
        note=None if not drift else "installed version differs from this SGR build",
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
