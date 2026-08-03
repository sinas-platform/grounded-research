from fastapi import APIRouter

from app.api.v1 import (
    annotations,
    answers,
    config,
    discovery,
    documents,
    entities,
    dossiers,
    health,
    info,
    ingestion,
    me,
    packages,
    playbooks,
    query_runs,
    relationships,
    result_filter,
    results,
    retrieval,
    runs,
    sinas_status,
    synthesis,
    uploads,
)
from app.services.sinas import get_sinas_auth

api_router = APIRouter(prefix="/api/v1")

# SDK-provided auth routes: POST /login, /verify-otp, /refresh, /logout, GET /me.
# Mounted under /api/v1/auth — frontend uses /api/v1/auth/login etc.
api_router.include_router(get_sinas_auth().router, prefix="/auth", tags=["auth"])

api_router.include_router(health.router)
api_router.include_router(info.router)
api_router.include_router(me.router)
api_router.include_router(config.router)
api_router.include_router(documents.router)
api_router.include_router(dossiers.router)
api_router.include_router(ingestion.router)
api_router.include_router(retrieval.router)
api_router.include_router(result_filter.router)
api_router.include_router(results.router)
api_router.include_router(synthesis.router)
api_router.include_router(answers.router)
api_router.include_router(relationships.router)
api_router.include_router(annotations.router)
api_router.include_router(entities.router)
api_router.include_router(playbooks.router)
api_router.include_router(query_runs.router)
api_router.include_router(packages.router)
api_router.include_router(sinas_status.router)
api_router.include_router(uploads.router)
api_router.include_router(runs.router)
api_router.include_router(discovery.router)
