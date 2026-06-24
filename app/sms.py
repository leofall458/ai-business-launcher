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
"""

import smtplib
from email.message import EmailMessage

from app.config import GMAIL_USER, GMAIL_APP_PASSWORD, ADMIN_PHONE_EMAIL

SMS_MAX_LENGTH = 160

def send_admin_sms(message: str) -> bool:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not ADMIN_PHONE_EMAIL:
        print(f"⚠️ Admin SMS not configured - skipping alert: {message}")
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
        return True
    except Exception as e:
        print(f"⚠️ Could not send admin SMS '{message}': {e}")
        return False
