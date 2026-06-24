"""Homegrown passwordless login for the customer dashboard - a signed,
single-use magic link plus a server-side session record, not a third-party
auth provider. Same stateless-token idiom already used twice in app/main.py
(_make_status_token, _make_ein_filing_link_token), reusing STATUS_SESSION_SECRET
since it's the same kind of server-side-only HMAC key and the old
status-token system that secret was created for is being retired by the
end of this rework anyway.

"customer_id" throughout this module is just the lowercased email address -
there's no separate customer collection in this app, orders carry their
own email field directly, and that's what app.main.get_owned_order
compares an authenticated session against.

Uses its own Firestore client (same pattern as app/ssn_vault.py) so
app.main can import this module without a circular import.
"""

import datetime
import hashlib
import hmac
import secrets

from google.cloud import firestore

from app.config import FIREBASE_PROJECT_ID, STATUS_SESSION_SECRET

MAGIC_LINK_TTL_SECONDS = 900  # 15 minutes - long enough to find the email, short enough to limit a leaked-inbox window
SESSION_IDLE_SECONDS = 3600  # 1 hour, refreshed on every authenticated request
SESSION_ABSOLUTE_SECONDS = 86400  # 24 hours, never refreshed - forces re-login at least once a day

BASE_URL = "https://app.launchbridge.ai"

_db = firestore.Client(project=FIREBASE_PROJECT_ID)
_MAGIC_LINKS = _db.collection("magic_link_tokens")
_SESSIONS = _db.collection("sessions")

def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)

def create_magic_link(email: str) -> str:
    """Issues a brand-new single-use login link for an email address. Does
    not check whether any order actually has this email - that ambiguity
    is intentional (see app.main's login route), so a failed lookup can't
    be distinguished from a successful one by response shape or timing."""
    token_id = secrets.token_urlsafe(32)
    expiry = int(_now().timestamp()) + MAGIC_LINK_TTL_SECONDS
    sig = hmac.new(STATUS_SESSION_SECRET.encode(), f"{token_id}:{expiry}".encode(), hashlib.sha256).hexdigest()

    _MAGIC_LINKS.document(token_id).set({
        "email": email.strip().lower(),
        "created_at": firestore.SERVER_TIMESTAMP,
        "expires_at": expiry,
        "redeemed_at": None,
    })
    return f"{BASE_URL}/dashboard/verify?token={token_id}&exp={expiry}&sig={sig}"

def redeem_magic_link(token_id: str, exp: str, sig: str) -> str | None:
    """Verifies the signature and expiry from the URL itself (no Firestore
    read needed to reject a tampered/expired link), then atomically
    enforces single-use via a transaction - a replayed link, even one
    that's still within its TTL, fails on the second attempt. Returns the
    associated email on success, None otherwise."""
    try:
        expiry = int(exp)
    except (TypeError, ValueError):
        return None
    expected_sig = hmac.new(STATUS_SESSION_SECRET.encode(), f"{token_id}:{expiry}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig or "", expected_sig):
        return None
    if expiry < int(_now().timestamp()):
        return None

    doc_ref = _MAGIC_LINKS.document(token_id)

    @firestore.transactional
    def _redeem(transaction):
        snap = doc_ref.get(transaction=transaction)
        if not snap.exists:
            return None
        data = snap.to_dict()
        if data.get("redeemed_at") is not None:
            return None
        transaction.update(doc_ref, {"redeemed_at": firestore.SERVER_TIMESTAMP})
        return data.get("email")

    return _redeem(_db.transaction())

def create_session(customer_id: str) -> str:
    """Always mints a fresh random session_id - logging in never reuses or
    upgrades an existing session, which is what "rotates on login" means
    here."""
    session_id = secrets.token_urlsafe(32)
    now = _now()
    _SESSIONS.document(session_id).set({
        "customer_id": customer_id,
        "created_at": now,
        "last_seen_at": now,
        "expires_at": now + datetime.timedelta(seconds=SESSION_ABSOLUTE_SECONDS),
        "idle_until": now + datetime.timedelta(seconds=SESSION_IDLE_SECONDS),
    })
    return session_id

def verify_and_touch_session(session_id: str) -> str | None:
    """Returns the customer_id for a still-valid session, refreshing its
    idle timeout as a side effect - or None if the session doesn't exist,
    or has passed either its absolute or idle expiry."""
    if not session_id:
        return None
    doc_ref = _SESSIONS.document(session_id)
    snap = doc_ref.get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    now = _now()
    if now > data["expires_at"] or now > data["idle_until"]:
        return None
    doc_ref.update({
        "last_seen_at": now,
        "idle_until": now + datetime.timedelta(seconds=SESSION_IDLE_SECONDS),
    })
    return data["customer_id"]

def delete_session(session_id: str) -> None:
    _SESSIONS.document(session_id).delete()
