from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response

from app.api.v1 import api_router
from app.config import get_settings
from app.services.sinas import get_management

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    settings = get_settings()
    logging.basicConfig(level=settings.grove_log_level)
    log.info("starting sinas-grove backend")
    log.info("auth mode: %s", settings.grove_auth_mode)
    if settings.grove_auth_mode == "simplified":
        log.warning(
            "GROVE_AUTH_MODE=simplified — every request runs as the admin "
            "identity (a presented bearer must still match SINAS_API_KEY). "
            "Local development only; never expose this mode beyond localhost."
        )
    log.info("sinas url: %s", settings.sinas_url)
    # No background workers: ingestion + discovery submit batches to Sinas
    # at API request time and reconcile via live-fetch on GET.
    yield
    await get_management().aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Sinas Grove",
        version="0.1.0",
        lifespan=lifespan,
    )

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        log.warning(
            "validation error on %s %s: %s", request.method, request.url.path, exc.errors()
        )
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    app.include_router(api_router)

    # Serve the SPA on every other path (single-image deploy).
    if STATIC_DIR.exists():
        assets_dir = STATIC_DIR / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str, request: Request) -> Response:  # noqa: ARG001
            # API routes are handled by the router above.
            if full_path.startswith("api/"):
                return Response(status_code=404)
            candidate = STATIC_DIR / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            index = STATIC_DIR / "index.html"
            if index.exists():
                return FileResponse(index)
            return Response("frontend not built", status_code=404)

    return app


app = create_app()
