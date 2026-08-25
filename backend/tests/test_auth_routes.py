"""The SDK's GET /me must not be reachable.

We mount the SDK auth router with auto_error=False. For a credential-less
request the SDK builds a client it calls "unauthenticated", but SinasClient's
constructor falls back to os.getenv("SINAS_API_KEY") — which is set in any
real deployment as the service identity — so that route answers anonymous
callers with the service account's id, email and roles. Observed on a public
deployment: HTTP 200 with the admin user's details and no Authorization
header present.

Our own /api/v1/me resolves the caller through get_caller and 401s, so the
fix is to drop the SDK's route rather than reimplement it.
"""

from __future__ import annotations

from app.api.v1 import api_router


def _paths() -> set[str]:
    return {getattr(r, "path", "") for r in api_router.routes}


def test_sdk_auth_me_is_not_mounted() -> None:
    assert "/api/v1/auth/me" not in _paths()


def test_own_me_is_mounted() -> None:
    # The frontend's session bootstrap calls this one; it must survive the
    # filtering above.
    assert "/api/v1/me" in _paths()


def test_other_sdk_auth_routes_survive() -> None:
    paths = _paths()
    for route in ("/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/logout"):
        assert route in paths, route
