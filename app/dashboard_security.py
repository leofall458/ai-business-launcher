"""Security headers for every route on this app, and CSRF tokens bound to
a dashboard session. Cache-Control: no-store stays scoped to the
authenticated /dashboard, /orders, and /admin paths - the public
marketing pages have no session/PII to protect and should stay cacheable.
The rest of the header set (CSP frame-ancestors, nosniff, Referrer-Policy,
X-XSS-Protection) applies everywhere, including the public marketing
pages: none of them need to be embeddable by a THIRD-PARTY origin, and
the protection costs nothing on a page with no session/PII either.

The public CSP is frame-ancestors 'self', not 'none' - the homepage and
/examples both embed the same-origin /examples/demo-site route in an
iframe (see index.html, examples.html) to show off a sample generated
site, and 'none' would silently blank that preview out exactly like the
stale X-Frame-Options: DENY bug documented in deployer.py's
_VERSION_CONFIG comment. /dashboard, /orders, and /admin have no such
same-origin iframe and keep the stricter 'none'.
"""

import hashlib
import hmac

from starlette.middleware.base import BaseHTTPMiddleware

from app.config import STATUS_SESSION_SECRET

DASHBOARD_PATH_PREFIXES = ("/dashboard", "/orders", "/admin")

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """setdefault, not direct assignment, so a route that already set a
    more specific header (e.g. a tighter CSP) is never overridden."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        is_dashboard = request.url.path.startswith(DASHBOARD_PATH_PREFIXES)
        if is_dashboard:
            response.headers.setdefault("Cache-Control", "no-store")
        frame_ancestors = "'none'" if is_dashboard else "'self'"
        response.headers.setdefault("Content-Security-Policy", f"frame-ancestors {frame_ancestors}")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")
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
