"""Security headers for the authenticated dashboard, and CSRF tokens bound
to a dashboard session - both scoped to the dashboard/document routes
rather than applied app-wide, since the marketing pages and the existing
admin panel have different (or no) requirements here.
"""

import hashlib
import hmac

from starlette.middleware.base import BaseHTTPMiddleware

from app.config import STATUS_SESSION_SECRET

DASHBOARD_PATH_PREFIXES = ("/dashboard", "/orders")

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """setdefault, not direct assignment, so a route that already set a
    more specific header (e.g. a tighter CSP) is never overridden."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(DASHBOARD_PATH_PREFIXES):
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

def make_csrf_token(session_id: str) -> str:
    """Deterministic per session_id rather than a fresh random value per
    request - simpler (no server-side storage, no race between issuing
    and checking) and just as effective since the session_id itself is
    already a high-entropy secret only the legitimate browser holds."""
    return hmac.new(STATUS_SESSION_SECRET.encode(), session_id.encode(), hashlib.sha256).hexdigest()

def verify_csrf_token(session_id: str, token: str) -> bool:
    if not session_id or not token:
        return False
    return hmac.compare_digest(token, make_csrf_token(session_id))
