"""Customer-facing order-milestone emails, sent via Gmail SMTP.

Every send_* function is wrapped in its own try/except and only ever logs on
failure - a flaky SMTP connection must never crash the background task (name
check, filing, EIN, asset generation) that triggered it. Credentials come
from Secret Manager (see app/config.py); if they're unset, sends are skipped
entirely rather than raising, so local dev without GMAIL_APP_PASSWORD still
works.

Every email is sent multipart/alternative - a plain-text body plus a
branded HTML body (see _wrap_html) - so it reads well in clients that prefer
either, and the dark-blue-header/white-card/CTA-button/footer chrome looks
the same across all of them.
"""

import smtplib
import mimetypes
from email.message import EmailMessage

from app.config import GMAIL_USER, GMAIL_APP_PASSWORD, SUPPORT_EMAIL
from app.dashboard_auth import create_magic_link

FROM_NAME = "Launch Bridge LLC"

def _send(to_email: str, subject: str, body: str, html_body: str = None,
          attachment_path: str = None, attachment_filename: str = None,
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
    if html_body:
        msg.add_alternative(html_body, subtype="html")

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

def _wrap_html(body_html: str, cta_text: str = None, cta_url: str = None) -> str:
    """Shared chrome for every email: dark blue header with the Launch
    Bridge name, white content card, an optional prominent CTA button, and
    a footer with the support contact + unsubscribe links."""
    cta_block = ""
    if cta_text and cta_url:
        cta_block = f"""
        <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding-top:12px;">
          <a href="{cta_url}" style="background-color:#2563eb;color:#ffffff;text-decoration:none;font-weight:600;
             font-size:15px;padding:14px 36px;border-radius:8px;display:inline-block;">{cta_text}</a>
        </td></tr></table>"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#f1f5f9;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f1f5f9;padding:32px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;max-width:560px;width:100%;">
<tr><td style="background-color:#0c2d6b;padding:28px 32px;">
<span style="color:#ffffff;font-size:21px;font-weight:700;letter-spacing:0.3px;">Launch Bridge LLC</span>
</td></tr>
<tr><td style="padding:32px;color:#1f2937;font-size:15px;line-height:1.65;">
{body_html}
{cta_block}
</td></tr>
<tr><td style="background-color:#f9fafb;padding:18px 32px;border-top:1px solid #e5e7eb;text-align:center;">
<p style="margin:0;color:#9ca3af;font-size:12px;">
Launch Bridge LLC &nbsp;|&nbsp;
<a href="mailto:support@launchbridge.ai" style="color:#9ca3af;text-decoration:underline;">support@launchbridge.ai</a> &nbsp;|&nbsp;
<a href="mailto:support@launchbridge.ai?subject=Unsubscribe" style="color:#9ca3af;text-decoration:underline;">Unsubscribe</a>
</p>
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""

def _ein_ascii_box(ein: str) -> str:
    """A plain-text-friendly bordered box around the EIN, for clients that
    render the plain-text body instead of the HTML alternative."""
    label = f"YOUR EIN: {ein}"
    width = len(label) + 4
    top = "┌" + "─" * width + "┐"
    middle = "│" + label.center(width) + "│"
    bottom = "└" + "─" * width + "┘"
    return f"{top}\n{middle}\n{bottom}"

def _info_table(rows: list) -> str:
    trs = "".join(
        f'<tr><td style="padding:8px 16px;color:#6b7280;font-size:13px;">{label}</td>'
        f'<td style="padding:8px 16px;font-size:14px;font-weight:600;color:#111827;">{value}</td></tr>'
        for label, value in rows
    )
    return f'<table style="width:100%;background:#f9fafb;border-radius:8px;margin:16px 0;border-collapse:collapse;">{trs}</table>'

def send_magic_link_email(email: str, magic_link_url: str):
    """The only email in the dashboard security rework that carries a
    live, usable link directly - that's the point of a magic link. Every
    other dashboard notification (see send_* below) links to the
    dashboard generically and relies on the customer already having a
    session or requesting a fresh link there."""
    body = (
        "Click the link below to sign in to your Launch Bridge dashboard:\n\n"
        f"{magic_link_url}\n\n"
        "This link expires in 15 minutes and can only be used once. If you "
        "didn't request this, you can safely ignore this email.\n\n"
        "- Launch Bridge LLC"
    )
    html_inner = (
        "<p>Click the button below to sign in to your Launch Bridge dashboard.</p>"
        "<p style=\"color:#6b7280;font-size:13px;\">This link expires in 15 minutes and can only be used once. "
        "If you didn't request this, you can safely ignore this email.</p>"
    )
    html = _wrap_html(html_inner, cta_text="Sign In", cta_url=magic_link_url)
    _send(email, "Sign in to Launch Bridge", body, html_body=html)

def send_order_received_email(order: dict, order_id: str):
    """Part 3: sent the moment an order moves draft -> paid. This is the
    very first dashboard sign-in link a customer gets - /success
    deliberately doesn't mint a session itself (completing Stripe
    Checkout proves payment, not email ownership), so this link is the
    real entry point."""
    email = order.get("email", "")
    name = order.get("full_name", "") or "there"
    business_name = order.get("business_name", "your business")
    url = create_magic_link(email)
    body = (
        f"Hi {name},\n\n"
        "Thank you for choosing Launch Bridge LLC!\n\n"
        "Your order has been confirmed. Here are your details:\n\n"
        f"Business Name: {business_name}\n"
        "Amount Paid: $350\n\n"
        "Sign in to your dashboard to track progress:\n"
        f"{url}\n\n"
        "What happens next:\n"
        "✅ Your documents are being generated now\n"
        "⏳ We will file your LLC with Virginia SCC within 24 hours\n"
        "⏳ EIN application follows LLC approval (1-3 business days)\n"
        "⏳ Your website will be live after LLC approval\n\n"
        f"Questions? Reply to this email or contact {SUPPORT_EMAIL}\n\n"
        "- Launch Bridge LLC"
    )
    html_inner = (
        f"<p>Hi {name},</p>"
        "<p>Thank you for choosing Launch Bridge LLC!</p>"
        "<p>Your order has been confirmed. Here are your details:</p>"
        + _info_table([("Business Name", business_name), ("Amount Paid", "$350")])
        + "<p style=\"margin-top:24px;font-weight:600;\">What happens next:</p>"
        "<p>✅ Your documents are being generated now<br>"
        "⏳ We will file your LLC with Virginia SCC within 24 hours<br>"
        "⏳ EIN application follows LLC approval (1-3 business days)<br>"
        "⏳ Your website will be live after LLC approval</p>"
        f"<p>Questions? Reply to this email or contact <a href=\"mailto:{SUPPORT_EMAIL}\">{SUPPORT_EMAIL}</a></p>"
    )
    html = _wrap_html(html_inner, cta_text="Sign In to Your Dashboard", cta_url=url)
    _send(email, f"Your Launch Bridge Order Confirmed - {business_name}", body, html_body=html)

def send_documents_ready_email(order: dict, order_id: str):
    """Email 2 (Part 4): sent the first time run_document_generation
    finishes every agent successfully for this order."""
    email = order.get("email", "")
    name = order.get("full_name", "") or "there"
    business_name = order.get("business_name", "your business")
    url = create_magic_link(email)
    body = (
        f"Hi {name},\n\n"
        "Your Articles of Organization, Operating Agreement, and brand kit are ready.\n\n"
        "View them on your dashboard:\n"
        f"{url}\n\n"
        f"Questions? Contact {SUPPORT_EMAIL}.\n\n"
        "- Launch Bridge LLC"
    )
    html_inner = (
        f"<p>Hi {name},</p>"
        "<p>Your Articles of Organization, Operating Agreement, and brand kit are ready.</p>"
        f"<p>Questions? Contact <a href=\"mailto:{SUPPORT_EMAIL}\">{SUPPORT_EMAIL}</a>.</p>"
    )
    html = _wrap_html(html_inner, cta_text="View Your Documents", cta_url=url)
    _send(email, f"Your LLC Documents are Ready - {business_name}", body, html_body=html)

def send_llc_filed_email(order: dict, order_id: str):
    """Email 3 (Part 4): sent right after run_scc_filing submits the
    Articles of Organization to the Virginia SCC."""
    email = order.get("email", "")
    name = order.get("full_name", "") or "there"
    business_name = order.get("business_name", "your business")
    url = create_magic_link(email)
    confirmation_number = order.get("scc_confirmation_number", "")
    confirmation_line = f"Confirmation #{confirmation_number}. " if confirmation_number else ""
    body = (
        f"Hi {name},\n\n"
        f"We have submitted your Articles of Organization to the Virginia SCC. {confirmation_line}"
        "Processing takes 1-3 business days.\n\n"
        "Track your order here:\n"
        f"{url}\n\n"
        f"Questions? Contact {SUPPORT_EMAIL}.\n\n"
        "- Launch Bridge LLC"
    )
    html_inner = (
        f"<p>Hi {name},</p>"
        f"<p>We have submitted your Articles of Organization to the Virginia SCC. {confirmation_line}"
        "Processing takes 1-3 business days.</p>"
        f"<p>Questions? Contact <a href=\"mailto:{SUPPORT_EMAIL}\">{SUPPORT_EMAIL}</a>.</p>"
    )
    html = _wrap_html(html_inner, cta_text="Track Your Order", cta_url=url)
    _send(email, f"Your LLC Has Been Filed - {business_name}", body, html_body=html)

def send_llc_approved_email(order: dict, order_id: str, confirmation_number: str = ""):
    """Email 4 (Part 4): sent once the Virginia SCC has actually approved
    the LLC. confirmation_number is still accepted (and stored on the order
    by every caller) so it's visible on the dashboard - this email's own
    wording follows the requested template, which doesn't repeat it.

    No longer attaches the certificate (see the dashboard security
    rework) - it's available for signed-URL download on the dashboard
    instead, so an intercepted email can't leak it directly."""
    email = order.get("email", "")
    name = order.get("full_name", "") or "there"
    business_name = order.get("business_name", "your business")
    url = create_magic_link(email)
    body = (
        f"Hi {name},\n\n"
        f"Great news! {business_name} LLC has been officially approved by the Virginia State Corporation Commission.\n\n"
        "Your certificate is on your dashboard. We are now applying for your EIN.\n\n"
        "View your dashboard here:\n"
        f"{url}\n\n"
        f"Questions? Contact {SUPPORT_EMAIL}.\n\n"
        "- Launch Bridge LLC"
    )
    html_inner = (
        f"<p>Hi {name},</p>"
        f"<p>🎉 Great news! <strong>{business_name} LLC</strong> has been officially approved by the "
        "Virginia State Corporation Commission.</p>"
        "<p>Your certificate is on your dashboard. We are now applying for your EIN.</p>"
        f"<p>Questions? Contact <a href=\"mailto:{SUPPORT_EMAIL}\">{SUPPORT_EMAIL}</a>.</p>"
    )
    html = _wrap_html(html_inner, cta_text="View Your Dashboard", cta_url=url)
    return _send(email, f"🎉 Your LLC is Approved! - {business_name}", body, html_body=html)

def forward_scc_approval_email(order: dict, order_id: str) -> bool:
    """Sent by app/gmail_poller.py the moment it matches a real SCC
    approval email to an order - distinct from send_llc_approved_email
    (used by the admin's manual fallback and the hourly name-search
    poller, see app/check_scc_status.py), which has its own already-
    shipped wording. This one uses the exact text requested for the
    email-forwarding flow specifically.

    No longer attaches the certificate - see send_llc_approved_email."""
    email = order.get("email", "")
    customer_name = order.get("full_name", "") or "there"
    url = create_magic_link(email)
    body = (
        f"Dear {customer_name}, Great news! Your LLC has been approved by the Virginia State "
        "Corporation Commission. Your Certificate of Organization is available on your dashboard. "
        f"Our team will now proceed with your EIN application.\n\n{url}\n\n- Launch Bridge LLC"
    )
    html_inner = (
        f"<p>Dear {customer_name},</p>"
        "<p>Great news! Your LLC has been approved by the Virginia State Corporation Commission. "
        "Your Certificate of Organization is available on your dashboard. Our team will now proceed "
        "with your EIN application.</p>"
    )
    html = _wrap_html(html_inner, cta_text="View Your Dashboard", cta_url=url)
    return _send(email, "Your LLC is approved!", body, html_body=html)

def send_ein_issued_email(order: dict, order_id: str, ein: str):
    """Email 5 (Part 4): sent once the EIN is issued by the IRS (or
    immediately, for skip_ein customers who already had one). Standard for
    every order the moment it has an EIN - see mark_ein_issued in
    app/main.py, the single place both the automatic IRS-scrape path and
    the admin's manual fallback both call through."""
    email = order.get("email", "")
    name = order.get("full_name", "") or "there"
    business_name = order.get("business_name", "your business")
    url = create_magic_link(email)
    ein_box = _ein_ascii_box(ein)
    body = (
        f"Hi {name},\n\n"
        f"Great news - your Employer Identification Number (EIN) has been issued by the IRS for {business_name}!\n\n"
        f"{ein_box}\n\n"
        "Save this number - you need it to:\n"
        "- Open a business bank account\n"
        "- Hire employees\n"
        "- File taxes\n"
        "- Apply for business licenses\n\n"
        "What happens next:\n"
        "⏳ We're generating your business website\n"
        "⏳ We're setting up your Stripe payment account\n\n"
        "Track your order here:\n"
        f"{url}\n\n"
        f"Questions? Contact {SUPPORT_EMAIL}.\n\n"
        "- Launch Bridge LLC\n"
        f"{SUPPORT_EMAIL}"
    )
    html_inner = (
        f"<p>Hi {name},</p>"
        f"<p>Great news - your Employer Identification Number (EIN) has been issued by the IRS for "
        f"<strong>{business_name}</strong>!</p>"
        '<table width="100%" cellpadding="0" cellspacing="0" style="margin:20px 0;">'
        '<tr><td align="center">'
        '<table cellpadding="0" cellspacing="0" style="border:2px solid #16a34a;border-radius:10px;background:#f0fdf4;">'
        '<tr><td style="padding:20px 36px;text-align:center;">'
        '<p style="margin:0;color:#15803d;font-size:12px;font-weight:700;letter-spacing:0.5px;text-transform:uppercase;">Your EIN</p>'
        f'<p style="margin:6px 0 0;color:#14532d;font-size:28px;font-weight:700;letter-spacing:1px;">{ein}</p>'
        "</td></tr></table>"
        "</td></tr></table>"
        "<p><strong>Save this number</strong> - you need it to:</p>"
        "<ul style=\"margin:0 0 16px;padding-left:20px;color:#1f2937;\">"
        "<li>Open a business bank account</li>"
        "<li>Hire employees</li>"
        "<li>File taxes</li>"
        "<li>Apply for business licenses</li>"
        "</ul>"
        "<p style=\"margin-top:24px;font-weight:600;\">What happens next:</p>"
        "<p>⏳ We're generating your business website<br>"
        "⏳ We're setting up your Stripe payment account</p>"
        f"<p>Questions? Contact <a href=\"mailto:{SUPPORT_EMAIL}\">{SUPPORT_EMAIL}</a>.</p>"
    )
    html = _wrap_html(html_inner, cta_text="Track Your Order", cta_url=url)
    _send(email, f"Your EIN is Ready - {business_name}", body, html_body=html)

def send_ein_filing_ready_email(order: dict, order_id: str, ein_filing_url: str):
    """Admin-facing (not customer-facing): sent by app/main.py's
    notify_ein_ready the moment an order has both SCC approval and a
    stored SSN - the only two prerequisites for EIN filing. The button
    links to a one-click, signed URL (see _make_ein_filing_link_token)
    that starts filing without the admin needing to log into the
    dashboard first - handy from a phone."""
    business_name = order.get("business_name", "this order")
    body = (
        f"SSN is on file for {business_name} - ready to file the EIN.\n\n"
        "Start filing here:\n"
        f"{ein_filing_url}\n\n"
        "(Make sure Chrome is open and logged into the IRS EIN Assistant session before clicking.)"
    )
    html_inner = (
        f"<p>SSN is on file for <strong>{business_name}</strong> - ready to file the EIN.</p>"
        "<p>Make sure Chrome is open and logged into the IRS EIN Assistant session before clicking below.</p>"
    )
    html = _wrap_html(html_inner, cta_text="Apply for EIN Now", cta_url=ein_filing_url)
    _send(GMAIL_USER, f"🔐 Ready to file EIN - {business_name}", body, html_body=html)

def send_website_live_email(order: dict, order_id: str):
    """Email 6 (Part 4): sent once run_asset_generation finishes the
    customer's website.

    No longer includes a raw Stripe Connect onboarding link - that URL
    now only ever appears inside the authenticated dashboard, generated
    on demand (see ensure_payment_link/connect_onboard in app/main.py),
    so it can't leak from an intercepted email."""
    email = order.get("email", "")
    name = order.get("full_name", "") or "there"
    business_name = order.get("business_name", "your business")
    website_url = order.get("website_url", "")
    url = create_magic_link(email)
    body = (
        f"Hi {name},\n\n"
        f"Your business website is now live at: {website_url}\n\n"
        "Share it with your customers!\n\n"
        "Visit your dashboard to finish setting up payments:\n"
        f"{url}\n\n"
        f"Questions? Contact {SUPPORT_EMAIL}.\n\n"
        "- Launch Bridge LLC"
    )
    html_inner = (
        f"<p>Hi {name},</p>"
        f"<p>Your business website is now live at: <a href=\"{website_url}\">{website_url}</a></p>"
        "<p>Share it with your customers!</p>"
        f"<p>Questions? Contact <a href=\"mailto:{SUPPORT_EMAIL}\">{SUPPORT_EMAIL}</a>.</p>"
    )
    html = _wrap_html(html_inner, cta_text="View Your Website", cta_url=website_url)
    _send(email, f"Your Business Website is Live! - {business_name}", body, html_body=html)

def send_visitor_message_email(business_name: str, visitor_name: str, visitor_email: str, message: str, to_email: str):
    """A lead from a deployed customer website's contact form (see the
    website templates' contact sections) - sent both to our own support
    inbox and, separately, straight to the business owner's email, since
    they're the one who actually needs to follow up with the visitor."""
    subject = f"New message from your website - {business_name}"
    body = (
        f"You have a new message from your {business_name} website:\n\n"
        f"From: {visitor_name} <{visitor_email}>\n\n"
        f"{message}\n\n"
        f"Reply directly to {visitor_email} to respond."
    )
    html_inner = (
        f"<p>You have a new message from your <strong>{business_name}</strong> website:</p>"
        + _info_table([("From", f"{visitor_name} ({visitor_email})")])
        + f"<p style=\"white-space:pre-wrap;\">{message}</p>"
        f"<p>Reply directly to <a href=\"mailto:{visitor_email}\">{visitor_email}</a> to respond.</p>"
    )
    html = _wrap_html(html_inner)
    _send(to_email, subject, body, html_body=html)

def send_ssn_expired_email(order: dict, order_id: str):
    """Sent by app.main's ssn_expiry_scheduler the moment it deletes an
    SSN that's been sitting encrypted for more than 72 hours without EIN
    filing having started - the customer re-enters it inline on the
    dashboard now (see the SSN-folding decision in the security-rework
    plan), not a separate /collect-ssn page."""
    email = order.get("email", "")
    name = order.get("full_name", "") or "there"
    url = create_magic_link(email)
    body = (
        f"Hi {name},\n\n"
        "For your security, we automatically delete stored information after 72 hours.\n\n"
        "Your SSN has been deleted and needs to be re-entered to complete your EIN application.\n\n"
        "Please re-enter it on your dashboard:\n"
        f"{url}\n\n"
        "- Launch Bridge LLC"
    )
    html_inner = (
        f"<p>Hi {name},</p>"
        "<p>For your security, we automatically delete stored information after 72 hours.</p>"
        "<p>Your SSN has been deleted and needs to be re-entered to complete your EIN application.</p>"
    )
    html = _wrap_html(html_inner, cta_text="Re-enter Your SSN", cta_url=url)
    _send(email, "Action Required - Re-enter SSN for EIN Application", body, html_body=html)

def send_everything_complete_email(order: dict, order_id: str):
    """Email 7 (Part 4): the final milestone email, sent once the order
    reaches the "complete" state - the full business package summary.

    No longer includes a raw Stripe Connect onboarding link - see
    send_website_live_email."""
    email = order.get("email", "")
    name = order.get("full_name", "") or "there"
    business_name = order.get("business_name", "your business")
    confirmation_number = order.get("scc_confirmation_number", "")
    ein = order.get("ein", "")
    website_url = order.get("website_url", "")
    connect_id = order.get("stripe_connect_account_id")
    url = create_magic_link(email)

    llc_line = f"✅ LLC: {business_name}" + (f" (Confirmation #{confirmation_number})" if confirmation_number else "")
    stripe_line = "✅ Stripe: complete setup on your dashboard to start accepting payments" if connect_id else "✅ Stripe: Not set up"

    body = (
        f"Hi {name},\n\n"
        "Congratulations! Everything is set up. Here is your complete business package:\n\n"
        f"{llc_line}\n"
        f"✅ EIN: {ein}\n"
        f"✅ Website: {website_url}\n"
        f"{stripe_line}\n\n"
        "Next steps:\n"
        "1. Complete your Stripe account setup to accept payments\n"
        "2. Open a business bank account using your EIN\n"
        "3. File your annual report with Virginia SCC each year\n\n"
        "Thank you for choosing Launch Bridge LLC!\n"
        "- The Launch Bridge Team"
    )
    html_inner = (
        f"<p>Hi {name},</p>"
        "<p>Congratulations! Everything is set up. Here is your complete business package:</p>"
        "<p>"
        f"{llc_line}<br>"
        f"✅ EIN: {ein}<br>"
        f"✅ Website: <a href=\"{website_url}\">{website_url}</a><br>"
        f"{stripe_line}"
        "</p>"
        "<p style=\"margin-top:24px;font-weight:600;\">Next steps:</p>"
        "<p>1. Complete your Stripe account setup to accept payments<br>"
        "2. Open a business bank account using your EIN<br>"
        "3. File your annual report with Virginia SCC each year</p>"
        "<p>Thank you for choosing Launch Bridge LLC!<br>- The Launch Bridge Team</p>"
    )
    html = _wrap_html(html_inner, cta_text="View Your Dashboard", cta_url=url)
    _send(email, f"🚀 {business_name} is Ready for Business!", body, html_body=html)
