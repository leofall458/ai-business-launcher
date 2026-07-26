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

script-src/connect-src give the SSN page's "no third-party scripts" rule
(see _brand.html's needs_ssn_entry guard around _clarity.html, and
_ga4.html's own guard) real policy backing instead of resting on those
Jinja conditionals alone - a future template regression that accidentally
reintroduces a tracker tag on that page would still be blocked by the
browser. The flag those conditionals already key off, needs_ssn_entry, is
computed deep inside a route handler (_dashboard_order_context in
app/main.py), not something this middleware can see from the URL alone -
it's threaded out via request.state.needs_ssn_entry, which the handler
sets before returning its response. This works because BaseHTTPMiddleware
and the route handler share the same ASGI scope, so request.state (backed
by scope["state"]) set inside call_next() is still visible here
afterward, even though dispatch() only sees a bare Request/Response pair.
Any route that never sets it (i.e. everything except the order dashboard)
defaults to False - permissive, tracking allowed - via getattr below.
"""

import hashlib
import hmac

from starlette.middleware.base import BaseHTTPMiddleware

from app.config import STATUS_SESSION_SECRET

DASHBOARD_PATH_PREFIXES = ("/dashboard", "/orders", "/admin")

# Actual hosts contacted by _ga4.html (GA4) and _clarity.html (Microsoft
# Clarity) - verified against live network traffic, not guessed: gtag.js's
# script tag + collect beacon, Clarity's script tag + its sharded collector
# subdomains (scripts./b./c.clarity.ms - Clarity load-balances across
# lettered subdomains) + its Bing/Ads correlation ping (Clarity is a
# Microsoft product and pings c.bing.com). No Google Ads client-side tag
# exists in this app today - ad conversion import is server-side (see
# app/google_ads_service.py) - but linking a Google Ads account to this GA4
# property can start client-side calls to *.doubleclick.net with no code
# change on this end, so it's allowlisted defensively here and is exactly
# what must stay excluded on the SSN page below.
TRACKING_SCRIPT_SRC = "https://www.googletagmanager.com https://www.clarity.ms https://scripts.clarity.ms"
TRACKING_CONNECT_SRC = (
    "https://www.google-analytics.com https://www.googletagmanager.com "
    "https://www.clarity.ms https://*.clarity.ms https://c.bing.com "
    "https://googleads.g.doubleclick.net"
)

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """setdefault, not direct assignment, so a route that already set a
    more specific header (e.g. a tighter CSP) is never overridden."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        is_dashboard = request.url.path.startswith(DASHBOARD_PATH_PREFIXES)
        if is_dashboard:
            response.headers.setdefault("Cache-Control", "no-store")
        frame_ancestors = "'none'" if is_dashboard else "'self'"

        # 'unsafe-inline' stays in both branches - this app's own JS is
        # almost entirely inline <script> blocks (onclick handlers, the GA4
        # snippet itself, htmx wiring), and CSP blocks ALL inline script
        # unless 'unsafe-inline' (or a nonce/hash) is present, regardless of
        # 'self'. Only the third-party origins differ between the two cases.
        needs_ssn_entry = getattr(request.state, "needs_ssn_entry", False)
        if needs_ssn_entry:
            script_src = "'self' 'unsafe-inline'"
            connect_src = "'self'"
        else:
            script_src = f"'self' 'unsafe-inline' {TRACKING_SCRIPT_SRC}"
            connect_src = f"'self' {TRACKING_CONNECT_SRC}"

        csp = f"frame-ancestors {frame_ancestors}; script-src {script_src}; connect-src {connect_src}"
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")
        # Cloud Run only ever terminates TLS - there's no legitimate plain-HTTP
        # version of this app for a browser to fall back to, so this is safe
        # unconditionally. No "preload": that requires submission to browsers'
        # built-in preload list, which is slow to undo if this domain's TLS
        # setup ever needs to change - max-age is enough on its own.
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
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
