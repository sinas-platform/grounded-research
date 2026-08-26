from fastapi import APIRouter

from app.api.v1 import (
    annotations,
    bulk,
    answers,
    config,
    discovery,
    documents,
    entities,
    dossiers,
    health,
    info,
    ingestion,
    maintenance,
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

# SDK-provided auth routes: POST /login, /verify-otp, /refresh, /logout.
# Mounted under /api/v1/auth — frontend uses /api/v1/auth/login etc.
#
# The SDK's GET /me is deliberately dropped. We mount its router with
# auto_error=False, and for a credential-less request the SDK hands the route
# a client it calls "unauthenticated" — but SinasClient's constructor falls
# back to os.getenv("SINAS_API_KEY"), which is set here as our service
# identity. The route therefore answers an anonymous caller with the service
# account's id, email and roles. Our own /api/v1/me (app.api.v1.me) resolves
# the caller through get_caller and 401s correctly; that is the one the
# frontend uses.
_sdk_auth_router = get_sinas_auth().router
_sdk_auth_router.routes = [
    r for r in _sdk_auth_router.routes if getattr(r, "path", None) != "/me"
]
api_router.include_router(_sdk_auth_router, prefix="/auth", tags=["auth"])

api_router.include_router(health.router)
api_router.include_router(info.router)
api_router.include_router(me.router)
api_router.include_router(config.router)
api_router.include_router(maintenance.router)
api_router.include_router(documents.router)
api_router.include_router(dossiers.router)
api_router.include_router(ingestion.router)
api_router.include_router(bulk.router)
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
