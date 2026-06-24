"""Customer-facing order-milestone emails, sent via Gmail SMTP.

Every send_* function is wrapped in its own try/except and only ever logs on
failure - a flaky SMTP connection must never crash the background task (name
check, filing, EIN, asset generation) that triggered it. Credentials come
from Secret Manager (see app/config.py); if they're unset, sends are skipped
entirely rather than raising, so local dev without GMAIL_APP_PASSWORD still
works.
"""

import smtplib
import mimetypes
from email.message import EmailMessage

from app.config import GMAIL_USER, GMAIL_APP_PASSWORD, SUPPORT_EMAIL

FROM_NAME = "Launch Bridge LLC"

def _send(to_email: str, subject: str, body: str, attachment_path: str = None, attachment_filename: str = None,
          attachment_bytes: bytes = None) -> bool:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print(f"⚠️ Gmail credentials not configured - skipping email '{subject}' to {to_email}")
        return False
    if not to_email:
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{GMAIL_USER}>"
    msg["To"] = to_email
    msg.set_content(body)

    if attachment_bytes is not None:
        ctype, _ = mimetypes.guess_type(attachment_filename or "")
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(attachment_bytes, maintype=maintype, subtype=subtype,
            filename=attachment_filename or "attachment")
    elif attachment_path:
        try:
            ctype, _ = mimetypes.guess_type(attachment_path)
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            with open(attachment_path, "rb") as f:
                msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                    filename=attachment_filename or attachment_path.split("/")[-1])
        except Exception as e:
            print(f"⚠️ Could not attach {attachment_path} to email '{subject}': {e}")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"⚠️ Could not send email '{subject}' to {to_email}: {e}")
        return False

def send_order_received_email(order: dict, order_id: str):
    email = order.get("email", "")
    business_name = order.get("business_name", "your business")
    body = (
        f"Hi {order.get('full_name', '')},\n\n"
        f"We received your payment for {business_name} - thank you!\n\n"
        "Here's what happens next:\n"
        "1. Immediately - our AI generates your documents, brand kit, and website\n"
        "2. Within 24 hours - our team files your LLC with the Virginia SCC\n"
        "3. 1-3 business days - Virginia SCC reviews and approves your LLC\n"
        "4. Same day as approval - we apply for your EIN with the IRS\n"
        "5. After your EIN - we set up your Stripe payment account\n"
        "6. Complete - you receive everything by email\n\n"
        f"Track your order anytime at: https://app.launchbridge.ai/status/{order_id}\n\n"
        f"Questions? Reply to this email or contact {SUPPORT_EMAIL}.\n\n"
        "- Launch Bridge LLC"
    )
    _send(email, "We received your order - here's what happens next", body)

def send_llc_filed_email(order: dict, order_id: str):
    email = order.get("email", "")
    business_name = order.get("business_name", "your business")
    body = (
        f"Hi {order.get('full_name', '')},\n\n"
        f"{business_name} has been submitted to the Virginia State Corporation Commission for filing.\n\n"
        "Virginia SCC typically reviews and approves new LLC filings within 1-3 business days. "
        "We'll email you again as soon as it's approved.\n\n"
        f"Track your order anytime at: https://app.launchbridge.ai/status/{order_id}\n\n"
        f"Questions? Contact {SUPPORT_EMAIL}.\n\n"
        "- Launch Bridge LLC"
    )
    _send(email, "Your LLC has been submitted to Virginia SCC", body)

def send_llc_approved_email(order: dict, order_id: str, confirmation_number: str = "",
                             certificate_path: str = None, certificate_bytes: bytes = None):
    email = order.get("email", "")
    business_name = order.get("business_name", "your business")
    confirmation_line = f"Virginia SCC confirmation number: {confirmation_number}\n\n" if confirmation_number else ""
    body = (
        f"Hi {order.get('full_name', '')},\n\n"
        f"Great news - {business_name} has been officially approved by the Virginia SCC!\n\n"
        f"{confirmation_line}"
        "We're now preparing your EIN application with the IRS - you'll hear from us again "
        "the same day it's issued.\n\n"
        f"Track your order anytime at: https://app.launchbridge.ai/status/{order_id}\n\n"
        f"Questions? Contact {SUPPORT_EMAIL}.\n\n"
        "- Launch Bridge LLC"
    )
    safe_name = business_name.replace(" ", "_").replace("/", "_")
    attachment_filename = f"{safe_name}_Certificate.pdf" if (certificate_bytes or certificate_path) else None
    return _send(email, "Your LLC is approved!", body,
        attachment_path=certificate_path, attachment_bytes=certificate_bytes,
        attachment_filename=attachment_filename)

def forward_scc_approval_email(order: dict, certificate_bytes: bytes = None) -> bool:
    """Sent by app/gmail_poller.py the moment it matches a real SCC
    approval email to an order - distinct from send_llc_approved_email
    (used by the admin's manual fallback and the hourly name-search
    poller, see app/check_scc_status.py), which has its own already-
    shipped wording. This one uses the exact text requested for the
    email-forwarding flow specifically."""
    email = order.get("email", "")
    business_name = order.get("business_name", "your business")
    customer_name = order.get("full_name", "") or "there"
    body = (
        f"Dear {customer_name}, Great news! Your LLC has been approved by the Virginia State "
        "Corporation Commission. Please find your Certificate of Organization attached. "
        "Our team will now proceed with your EIN application. - Launch Bridge LLC"
    )
    safe_name = business_name.replace(" ", "_").replace("/", "_")
    return _send(email, "Your LLC is approved!", body,
        attachment_bytes=certificate_bytes,
        attachment_filename=f"{safe_name}_Certificate.pdf" if certificate_bytes else None)

def send_ein_issued_email(order: dict, order_id: str, ein: str):
    email = order.get("email", "")
    business_name = order.get("business_name", "your business")
    body = (
        f"Hi {order.get('full_name', '')},\n\n"
        f"Your EIN for {business_name} has been issued.\n\n"
        f"Your EIN is: {ein}\n\n"
        "Keep this number somewhere safe - you'll need it for opening a business bank account, "
        "filing taxes, and applying for licenses or permits.\n\n"
        "Next, we're setting up your Stripe payment account so you can start accepting payments.\n\n"
        f"Track your order anytime at: https://app.launchbridge.ai/status/{order_id}\n\n"
        f"Questions? Contact {SUPPORT_EMAIL}.\n\n"
        "- Launch Bridge LLC"
    )
    _send(email, f"Your EIN is {ein}", body)

def send_website_live_email(order: dict, order_id: str):
    email = order.get("email", "")
    business_name = order.get("business_name", "your business")
    website_url = order.get("website_url", "")
    body = (
        f"Hi {order.get('full_name', '')},\n\n"
        f"{business_name} is fully set up and your website is live!\n\n"
        f"Visit it here: {website_url}\n\n"
        "Your full business package - LLC documents, brand kit, marketing plan, EIN, and "
        "Stripe payment account - is ready and waiting for you.\n\n"
        f"View everything anytime at: https://app.launchbridge.ai/status/{order_id}\n\n"
        f"Questions? Contact {SUPPORT_EMAIL}.\n\n"
        "- Launch Bridge LLC"
    )
    _send(email, "Your website is live!", body)
