"""Admin SMS alerts via email-to-SMS, reusing the same Gmail SMTP
credentials as app/email_service.py - no separate SMS provider/API needed.

Carrier email-to-SMS gateways (ADMIN_PHONE_EMAIL is a tmomail.net address
for T-Mobile) render the email body as the text message and ignore the
subject entirely, so every message here is sent with no subject and kept
under the ~160-char SMS limit - anything longer gets truncated rather than
silently arriving as multiple split texts.

Like app/email_service.py, every call is wrapped in try/except and only
ever logs on failure - a flaky SMTP connection must never crash the
background task (payment, filing, EIN) that triggered the alert.

Every attempt (success or failure) is also logged to Firestore
(SMS_LOG_COLLECTION) - a stdout print is invisible unless someone is
already tailing Cloud Run logs at the exact moment it happens, and SMTP
happily returns success even when the receiving carrier gateway silently
drops the message, so a print-only failure log isn't enough to notice a
real alert went missing (see the two live orders that never reached the
admin's phone despite SMTP raising no error)."""

import datetime
import smtplib
from email.message import EmailMessage

from google.cloud import firestore

from app.config import APP_ENV, FIREBASE_PROJECT_ID, GMAIL_USER, GMAIL_APP_PASSWORD, ADMIN_PHONE_EMAIL, SMS_LOG_COLLECTION

SMS_MAX_LENGTH = 160

_log_client = None

def _log_attempt(message: str, success: bool, error: str = None) -> None:
    # Own client rather than reusing app.main's `db` - app.main imports
    # this module, so importing back would be circular. Lazily created so
    # importing app.sms (e.g. from a script) never pays for a Firestore
    # connection unless send_admin_sms actually runs.
    global _log_client
    try:
        if _log_client is None:
            _log_client = firestore.Client(project=FIREBASE_PROJECT_ID)
        _log_client.collection(SMS_LOG_COLLECTION).add({
            "message": message,
            "success": success,
            "error": error,
            "admin_phone_email": ADMIN_PHONE_EMAIL,
            "app_env": APP_ENV,
            "sent_at": datetime.datetime.now(datetime.timezone.utc),
        })
    except Exception as e:
        # Logging the attempt must never be why the attempt itself looks
        # like it crashed the caller.
        print(f"⚠️ Could not log admin SMS attempt to Firestore: {e}")

def send_admin_sms(message: str) -> bool:
    # Every admin text gets an environment tag so a staging test never gets
    # mistaken for a real customer/payment - callers that already build
    # their own [LIVE]/[TEST] wording (see the payment-confirmed and
    # signup-complete alerts in main.py) are left alone rather than tagged
    # twice.
    if "[LIVE]" not in message and "[TEST]" not in message:
        prefix = "🧪 [TEST] " if APP_ENV == "staging" else "💰 [LIVE] "
        message = prefix + message

    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not ADMIN_PHONE_EMAIL:
        print(f"⚠️ Admin SMS not configured - skipping alert: {message}")
        _log_attempt(message, success=False, error="not configured (missing GMAIL_USER/GMAIL_APP_PASSWORD/ADMIN_PHONE_EMAIL)")
        return False

    if len(message) > SMS_MAX_LENGTH:
        message = message[:SMS_MAX_LENGTH - 1] + "…"

    msg = EmailMessage()
    msg["From"] = GMAIL_USER
    msg["To"] = ADMIN_PHONE_EMAIL
    msg["Subject"] = ""
    msg.set_content(message)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        _log_attempt(message, success=True)
        return True
    except Exception as e:
        print(f"⚠️ Could not send admin SMS '{message}': {e}")
        _log_attempt(message, success=False, error=str(e))
        return False
