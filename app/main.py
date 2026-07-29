import os
import re
import io
import json
import zipfile
import secrets
import hmac
import hashlib
import asyncio
import datetime
from urllib.parse import quote, unquote
from google.cloud import firestore
from fastapi import FastAPI, Request, BackgroundTasks, Form, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from app.config import (
    FIREBASE_PROJECT_ID, ADMIN_PASSWORD, ORDERS_COLLECTION, STATUS_SESSION_SECRET,
    APP_ENV, SUPPORT_EMAIL, GOOGLE_PLACES_API_KEY, GOOGLE_ANALYTICS_ID, CLARITY_ID, SAMPLE_WEBSITE_URL,
    STRIPE_PUBLISHABLE_KEY, PAGE_VIEWS_COLLECTION,
    AGENT_EVENTS_COLLECTION, ORDER_RUNS_COLLECTION, DAILY_METRICS_COLLECTION,
)
from app.agents.name_agent import screen_business_name, generate_name_ideas
from app.agents.category_agent import classify_business_category, CATEGORY_TAXONOMY, LOW_CONFIDENCE_THRESHOLD
from app.agents.scc_name_check import (
    check_name_on_scc, check_name_public, check_llc_exists_on_scc, sanitize_business_name,
    run_scc_health_check,
)
from app.agents.llc_agent import generate_llc_paperwork
from app.agents.brand_agent import generate_brand_kit, generate_logo_variations, generate_favicon_svg, svg_to_data_uri
from app.agents.marketing_agent import generate_marketing_plan, build_marketing_plan_pdf
from app.agents.pdf_agent import generate_llc_pdf
from app.scc_llc_filer import file_llc_on_scc, verify_name_before_filing, NameTakenError
from app.ein_filer import file_ein_with_irs
from app.utils.irs_hours import is_irs_open, next_irs_open, format_eta
from app.secrets import preload as preload_secrets
from app.agents.website_agent import generate_website, render_website_html, TEMPLATE_DEFAULT_COLORS, get_backdrop_image_options
from app.deployer import deploy_website, make_site_id, get_website_html, check_iframe_embeddable
from app import ai_ops
from app.photo_utils import process_photo, MAX_UPLOAD_BYTES
from app.stripe_service import (
    create_checkout_session,
    retrieve_checkout_session,
    create_connect_account,
    create_account_link,
    create_pay_what_you_want_payment_link,
    is_account_active,
    check_and_update_website,
    construct_webhook_event,
)
from app.config import LLC_FORMATION_PRICE_CENTS, RA_ANNUAL_FEE_CENTS
from app.google_ads_service import upload_click_conversion
from app.ssn_vault import (
    encrypt_ssn, decrypt_ssn, delete_ssn, ssn_age_hours, is_ssn_stored,
)
from app.log_scrub import scrub_ssn
from app.validators import (
    validate_business_idea, validate_step4_details, validate_post_payment_intake,
    validate_ssn, CHECKOUT_VALIDATED_FIELDS, POST_PAYMENT_VALIDATED_FIELDS,
)
from app.email_service import (
    send_order_received_email,
    send_early_assets_email,
    send_llc_filed_email,
    send_llc_approved_email,
    send_ein_issued_email,
    send_ein_filing_ready_email,
    send_website_live_email,
    send_everything_complete_email,
    send_ssn_expired_email,
    send_name_rejected_email,
    send_magic_link_email,
    send_visitor_message_email,
    send_abandoned_cart_email_1h,
    send_abandoned_cart_email_24h,
    send_mid_flow_recovery_email,
)
from app.document_store import upload_document, generate_signed_url
from app.sms import send_admin_sms
from app.dashboard_auth import (
    create_magic_link, redeem_magic_link, create_session,
    verify_and_touch_session, delete_session, SESSION_ABSOLUTE_SECONDS,
    BASE_URL as DASHBOARD_BASE_URL,
)
from app.dashboard_security import SecurityHeadersMiddleware, make_csrf_token, verify_csrf_token

app = FastAPI(title="Launch Bridge LLC")
app.add_middleware(SecurityHeadersMiddleware)

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["app_env"] = APP_ENV
templates.env.globals["google_analytics_id"] = GOOGLE_ANALYTICS_ID
templates.env.globals["clarity_id"] = CLARITY_ID
templates.env.globals["sample_website_url"] = SAMPLE_WEBSITE_URL
templates.env.globals["category_taxonomy"] = CATEGORY_TAXONOMY
templates.env.globals["category_low_confidence_threshold"] = LOW_CONFIDENCE_THRESHOLD
app.mount("/static", StaticFiles(directory="app/static"), name="static")

db = firestore.Client(project=FIREBASE_PROJECT_ID)
ORDERS = db.collection(ORDERS_COLLECTION)
LEADS = db.collection("leads")
DOCUMENT_ACCESS_LOG = db.collection("document_access_log")
ERRORS = db.collection("errors")
AGENT_EVENTS = db.collection(AGENT_EVENTS_COLLECTION)
ORDER_RUNS = db.collection(ORDER_RUNS_COLLECTION)
DAILY_METRICS = db.collection(DAILY_METRICS_COLLECTION)

# The one message every customer-facing error path collapses to - never
# stack traces, HTTP codes, or raw exception/library text. Admins still see
# full detail via the Errors section on /admin (see log_customer_error
# below) and the existing raw order.*_error fields there.
CUSTOMER_FRIENDLY_ERROR = f"We're working on your request. If you have any questions contact {SUPPORT_EMAIL}."
NOT_FOUND_MESSAGE = "We couldn't find that page."

def _is_htmx_request(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"

def log_customer_error(request: Request, exc: Exception) -> None:
    """Records every unhandled/500-level error to Firestore (see the
    Errors section on /admin) and pages the admin - the only place a
    customer ever sees the real exception is here, in a place only admins
    can read. order_id is best-effort (most order-scoped routes carry it
    as a path param; not every route has one)."""
    order_id = request.path_params.get("order_id")
    try:
        ERRORS.add({
            "timestamp": firestore.SERVER_TIMESTAMP,
            "url": str(request.url),
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:2000],
            "order_id": order_id,
            "user_agent": request.headers.get("user-agent", ""),
            "resolved": False,
            "app_env": APP_ENV,
        })
    except Exception as log_err:
        print(f"⚠️ Could not log error to Firestore: {log_err}")

    print(f"🔥 {type(exc).__name__} on {request.url.path}: {exc}")
    try:
        send_admin_sms(f"⚠️ Error on Launch Bridge: {request.url.path} - check Firestore errors collection")
    except Exception as sms_err:
        print(f"⚠️ Could not send admin SMS for error: {sms_err}")

def friendly_error_response(request: Request, message: str, status_code: int, headers: dict = None):
    """Every error response a customer can see funnels through here - a
    full branded page for a normal page load, or a small inline snippet
    for an htmx partial request (returning the full page would get
    injected into whatever fragment htmx was targeting, which looks
    broken - see Part 7 of the error-handling rework).

    headers must be forwarded through, not dropped - a 401 with no
    WWW-Authenticate header looks identical to any other error page, but
    browsers won't prompt for Basic Auth credentials without it, which
    would silently lock the admin out of /admin entirely."""
    if _is_htmx_request(request):
        return templates.TemplateResponse(request, "_error_snippet.html",
            {"message": message}, status_code=status_code, headers=headers)
    return templates.TemplateResponse(request, "error_friendly.html",
        {"message": message, "support_email": SUPPORT_EMAIL}, status_code=status_code, headers=headers)

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Covers every deliberate `raise HTTPException(...)` in the app, plus
    Starlette's own 404 for unmatched routes. Only 5xx is treated as a
    real, unexpected error worth paging the admin over - a 404 or a 403
    from a bad/expired link is normal traffic, not an incident."""
    if exc.status_code >= 500:
        log_customer_error(request, exc)
        message = "We hit a small snag. Our team has been notified and will fix it shortly."
    elif exc.status_code == 404:
        message = NOT_FOUND_MESSAGE
    else:
        message = CUSTOMER_FRIENDLY_ERROR
    return friendly_error_response(request, message, exc.status_code, headers=exc.headers)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """FastAPI's default 422 response body is a raw, field-by-field
    Pydantic validation dump - technical detail no customer should see."""
    return friendly_error_response(request, CUSTOMER_FRIENDLY_ERROR, 422)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """The actual crash backstop - anything that reaches here is a genuine
    bug (unlike the deliberate HTTPExceptions above), so it's always
    logged and always pages the admin."""
    log_customer_error(request, exc)
    return friendly_error_response(
        request, "We hit a small snag. Our team has been notified and will fix it shortly.", 500)

@app.on_event("startup")
async def on_startup():
    preload_secrets()
    # This scheduler drives the same local-Chrome Playwright automation as
    # run_scc_filing/run_ein_filing - on a deployed server with no path to
    # that browser (e.g. Cloud Run), it can only fail every 5 minutes and
    # write a misleading "crashed unexpectedly" error onto real orders.
    # Disable it there and rely on app/local_filing_poller.py instead.
    if os.getenv("ENABLE_EIN_SCHEDULER", "true").lower() == "true":
        asyncio.create_task(ein_queue_scheduler())
    # Pure Firestore/KMS work, no Playwright/CDP involved - safe to run
    # everywhere (Cloud Run included), unlike the scheduler above.
    asyncio.create_task(ssn_expiry_scheduler())
    asyncio.create_task(stripe_activation_scheduler())
    asyncio.create_task(abandoned_cart_scheduler())
    asyncio.create_task(scc_health_check_scheduler())

# Canonical order of the order state machine. An order's "state" field is
# always one of these. Progression is mostly linear, but filing_confirmed
# and ein_requested/ein_issued can land out of this textbook order in real
# wall-clock time (SCC and the IRS move at their own pace) - the admin
# dashboard lets the admin set filing_confirmed and ein_issued independently
# of where the automated steps currently are.
ORDER_STATES = [
    "draft", "paid", "name_selected", "intake_complete",
    "assets_generating", "assets_complete",
    "review_approved", "name_rejected", "filing_submitted",
    "filing_confirmed", "awaiting_ein_filing", "ein_requested", "ein_issued",
    "finalizing", "complete",
]
ORDER_STATE_INDEX = {s: i for i, s in enumerate(ORDER_STATES)}
# name_rejected is a "go back" outcome (SCC rejected the name at filing
# time - see verify_name_before_filing/NameTakenError), not a forward
# ordinal milestone - its position in ORDER_STATES only exists so
# ORDER_STATE_INDEX[state] lookups don't KeyError; every place that cares
# whether an order needs to re-pick a name checks state == "name_rejected"
# explicitly (see next_incomplete_step_url, step_label, dashboard_name)
# rather than relying on reached()'s ordinal comparison.

def reached(state: str, milestone: str) -> bool:
    """True once an order's state has reached or passed the given
    milestone - states only move forward, so an ordinal compare is enough."""
    return ORDER_STATE_INDEX.get(state, 0) >= ORDER_STATE_INDEX[milestone]

def record_state(order_ref, new_state: str, **extra_fields):
    """Every state transition goes through here: updates order.state (the
    live source of truth the rest of the app reads) and appends a
    timestamped entry to the order's events subcollection in the same
    call - a permanent, append-only audit trail that survives even if the
    main document is later overwritten or misread. extra_fields are merged
    onto the order doc (e.g. paid_at, filing_error clears) but not
    duplicated into the event - the event is just "what state, when"."""
    # extra_fields last so it can never accidentally clobber the intended
    # state - e.g. /launch spreads the raw intake form in here, and a
    # future address field literally named "state" (a US state dropdown)
    # would otherwise silently overwrite it via dict-literal precedence.
    order_ref.set({**extra_fields, "state": new_state}, merge=True)
    order_ref.collection("events").add({"state": new_state, "at": firestore.SERVER_TIMESTAMP})

STATE_MESSAGES = {
    "draft": "We're waiting for your payment to go through.",
    "paid": "Payment received! Let's pick your business name.",
    "name_selected": "Business name verified! Finish a few more details so we can start building your business.",
    "intake_complete": "We're building your business package — brand kit, website, and documents are on the way.",
    "assets_generating": "We're building your business package — brand kit, website, and documents are on the way.",
    "review_approved": "Your brand kit and documents are generating and we're preparing to file your LLC.",
    # assets_complete, filing_submitted, finalizing, filing_confirmed,
    # ein_requested, and ein_issued all have dynamic wording - see
    # compute_state_message() below - since whether the website is actually
    # live (order.website_url - now admin-approved, not automatic) or the
    # EIN status/value aren't reflected by the state name alone.
    "complete": "Everything is ready — here's your full business package.",
}

def compute_state_message(order: dict, state: str) -> str:
    """Most states have a fixed message (STATE_MESSAGES). A handful need
    live data instead - whether the website has actually been generated
    and approved yet (order.website_url), whether the EIN is queued on IRS
    hours, or the actual EIN once issued - so they're built here."""
    website_ready = bool(order.get("website_url"))
    if state == "assets_complete":
        if website_ready:
            return "Your brand kit, marketing plan, and business website are ready. We're preparing to file your LLC."
        return "Your brand kit and marketing plan are ready. We're preparing to file your LLC."
    if state == "filing_submitted":
        if website_ready:
            return "Your brand kit, website, and documents are ready! Your LLC has been filed with Virginia SCC — approval typically takes 1-3 business days."
        return "Your brand kit and documents are ready! Your LLC has been filed with Virginia SCC — approval typically takes 1-3 business days."
    if state == "finalizing":
        if website_ready:
            return "Your brand kit, marketing plan, and business website are ready."
        return "Your brand kit and marketing plan are ready."
    if state in ("filing_confirmed", "awaiting_ein_filing"):
        website_line = "Your website is live! " if website_ready else ""
        if order.get("ein_status") == "queued" and order.get("next_available_window"):
            window = datetime.datetime.fromisoformat(order["next_available_window"])
            return (
                f"Your LLC is approved! {website_line}Your EIN application will be submitted on "
                f"{window.strftime('%A, %B %d')} at {window.strftime('%I:%M %p').lstrip('0')} Eastern "
                f"when the IRS system opens."
            )
        return f"Your LLC is officially approved! {website_line}We're now applying for your EIN."
    if state == "ein_requested":
        return "Your EIN application has been submitted to the IRS. You will receive your EIN shortly."
    if state == "ein_issued":
        return f"Your EIN is: {order.get('ein', '')}. Your business is fully ready to operate!"
    if state == "name_rejected":
        return (
            f"\"{order.get('business_name', 'Your chosen name')}\" was rejected by Virginia SCC - "
            "please log in and choose a new name to continue."
        )
    return STATE_MESSAGES.get(state, "")

def step_label(order: dict) -> str:
    """Human label for the admin dashboard's per-order badge, so support can
    tell at a glance which of the 7 customer-facing steps an order is stuck
    on - maps the ordinal state plus the awaiting_intake/awaiting_ssn flags
    (which don't show up in `state` itself) back to that step."""
    state = order.get("state", "draft")
    if state == "draft":
        return "Step 2: Payment"
    if state == "name_rejected":
        return "Step 3: Name rejected — needs new name"
    if not reached(state, "name_selected"):
        return "Step 3: Naming"
    if order.get("awaiting_intake"):
        return "Step 5: Business Details" if order.get("full_name") else "Step 4: Your Info"
    if order.get("awaiting_ssn") and needs_ssn(order):
        return "Step 7: SSN"
    if state == "complete":
        return "Done"
    return "Processing"

def next_incomplete_step_url(order_id: str, order: dict) -> str | None:
    """Where to send a customer who lands on the dashboard (magic link,
    /dashboard/orders resolution, or the order page itself) while still
    mid-wizard - None once Steps 3-5 are all done, meaning the regular
    status page (dashboard_order.html) is the right place to show them.
    Mirrors step_label's logic since both answer "what step are they on,"
    just as a URL instead of a display string."""
    state = order.get("state", "draft")
    # name_rejected can happen well after Steps 3-5 (SCC rejects at filing
    # time, which is admin-triggered post-intake) - checked before the
    # ordinal reached() comparison since name_rejected's list position
    # doesn't reflect a real forward-progress point (see ORDER_STATES).
    if state == "name_rejected":
        return f"/dashboard/orders/{order_id}/name"
    if not reached(state, "name_selected"):
        return f"/dashboard/orders/{order_id}/name"
    if order.get("awaiting_intake"):
        return f"/dashboard/orders/{order_id}/business" if order.get("full_name") else f"/dashboard/orders/{order_id}/details"
    return None

def fmt_date(ts) -> str:
    """Firestore SERVER_TIMESTAMP fields come back as datetimes; the
    IRS-queue window is stored as an isoformat string - normalize both."""
    if not ts:
        return ""
    if isinstance(ts, str):
        try:
            ts = datetime.datetime.fromisoformat(ts)
        except ValueError:
            return ""
    return ts.strftime("%B %-d, %Y")

def on_date(ts) -> str:
    formatted = fmt_date(ts)
    return f" on {formatted}" if formatted else ""

def compute_timeline(order: dict, state: str) -> list:
    """Derives the 9-step customer-facing timeline. Assets (brand kit,
    website, Stripe) are shown first because they complete immediately
    after intake — independent of LLC filing and EIN, which come after.
    skip_llc_formation/skip_ein customers bypass whole government steps."""
    skip_llc = bool(order.get("skip_llc_formation"))
    skip_ein = bool(order.get("skip_ein"))
    business_name = order.get("business_name", "your business")
    email = order.get("email", "")
    assets_status = order.get("assets_status", "")
    intake_done = not order.get("awaiting_intake")
    steps = []

    # 1. Payment Received
    if state != "draft":
        amount_label = f"${LLC_FORMATION_PRICE_CENTS // 100}"
        steps.append({"key": "payment", "name": "Payment Received", "status": "complete",
            "description": f"Payment of {amount_label} confirmed{on_date(order.get('paid_at'))}"})
    else:
        steps.append({"key": "payment", "name": "Payment Received", "status": "pending",
            "description": "Waiting for payment to go through."})

    # 2. Setup Complete (intake form)
    if state == "draft":
        steps.append({"key": "setup", "name": "Setup Complete", "status": "pending",
            "description": "You'll fill out your address, website preferences, and documents after payment."})
    elif intake_done:
        steps.append({"key": "setup", "name": "Setup Complete", "status": "complete",
            "description": f"Business details collected{on_date(order.get('intake_complete_at'))}"})
    else:
        steps.append({"key": "setup", "name": "Setup Complete", "status": "current",
            "description": "Check your email for a link to complete your setup form."})

    # 3. Brand Kit & Documents Ready (generated immediately after intake)
    if order.get("documents_generated") or assets_status == "complete":
        steps.append({"key": "documents", "name": "Brand Kit & Documents Ready", "status": "complete",
            "description": f"Articles of Organization, Operating Agreement, brand kit, and marketing plan created{on_date(order.get('documents_generated_at'))}"})
    elif order.get("documents_error"):
        steps.append({"key": "documents", "name": "Brand Kit & Documents Ready", "status": "on_hold",
            "description": CUSTOMER_FRIENDLY_ERROR})
    elif intake_done:
        steps.append({"key": "documents", "name": "Brand Kit & Documents Ready", "status": "current",
            "description": "AI is generating your brand kit, documents, and marketing plan — usually 2-3 minutes..."})
    else:
        steps.append({"key": "documents", "name": "Brand Kit & Documents Ready", "status": "pending",
            "description": "Generated immediately when you complete setup."})

    # 4. Website Live (built by our team and reviewed before it goes live -
    # see run_website_generation/run_website_approval - not automatic)
    website_url = order.get("website_url")
    if website_url:
        steps.append({"key": "website", "name": "Business Website Live", "status": "complete",
            "description": f"Your website is live at {website_url}", "url": website_url})
    elif order.get("asset_generation_error") and intake_done:
        steps.append({"key": "website", "name": "Business Website Live", "status": "on_hold",
            "description": CUSTOMER_FRIENDLY_ERROR})
    elif order.get("website_review_status") == "pending_review":
        steps.append({"key": "website", "name": "Business Website Live", "status": "current",
            "description": "Your website has been built and is being finalized by our team — it'll be live shortly."})
    elif intake_done:
        steps.append({"key": "website", "name": "Business Website Live", "status": "current",
            "description": "Our team is preparing your business website."})
    else:
        steps.append({"key": "website", "name": "Business Website Live", "status": "pending",
            "description": "Website will be created after you complete setup."})

    # 5. Stripe Payment Account (created immediately after intake)
    connect_id = order.get("stripe_connect_account_id")
    if connect_id:
        if order.get("payment_button_live"):
            steps.append({"key": "stripe", "name": "Stripe Payment Account", "status": "complete",
                "description": "✅ Payments are live on your website!"})
        elif is_account_active(connect_id):
            steps.append({"key": "stripe", "name": "Stripe Payment Account", "status": "current",
                "description": "✅ Your Stripe account is active - adding your payment button to your website now..."})
        else:
            steps.append({"key": "stripe", "name": "Stripe Payment Account", "status": "current",
                "description": "⏳ Complete your Stripe setup to activate payments on your website",
                "onboarding": True})
    elif intake_done:
        steps.append({"key": "stripe", "name": "Stripe Payment Account", "status": "current",
            "description": "Creating your Stripe payment account..."})
    else:
        steps.append({"key": "stripe", "name": "Stripe Payment Account", "status": "pending",
            "description": "Stripe account will be created after you complete setup."})

    # 6. LLC Filed with Virginia SCC
    if skip_llc:
        steps.append({"key": "filed", "name": "LLC Filed with Virginia SCC", "status": "complete",
            "description": "Skipped — using your existing LLC"})
    elif order.get("filing_error"):
        steps.append({"key": "filed", "name": "LLC Filed with Virginia SCC", "status": "on_hold",
            "description": CUSTOMER_FRIENDLY_ERROR})
    elif reached(state, "filing_submitted"):
        steps.append({"key": "filed", "name": "LLC Filed with Virginia SCC", "status": "complete",
            "description": f"Filed with the Virginia SCC{on_date(order.get('filing_submitted_at'))}"})
    elif state == "review_approved":
        steps.append({"key": "filed", "name": "LLC Filed with Virginia SCC", "status": "current",
            "description": "Your LLC is being submitted to the Virginia SCC..."})
    elif reached(state, "name_selected"):
        steps.append({"key": "filed", "name": "LLC Filed with Virginia SCC", "status": "current",
            "description": "Our team is reviewing and will file your LLC within 72 hours."})
    else:
        steps.append({"key": "filed", "name": "LLC Filed with Virginia SCC", "status": "pending",
            "description": "Will be filed within 72 hours of setup completion."})

    # 7. LLC Approved by Virginia
    if skip_llc:
        steps.append({"key": "approved", "name": "LLC Approved by Virginia", "status": "complete",
            "description": "Skipped — your existing LLC is already approved"})
    elif reached(state, "filing_confirmed"):
        steps.append({"key": "approved", "name": "LLC Approved by Virginia", "status": "complete",
            "description": f"Virginia approved {business_name}{on_date(order.get('filing_confirmed_at'))}"})
    else:
        steps.append({"key": "approved", "name": "LLC Approved by Virginia",
            "status": "current" if state == "filing_submitted" else "pending",
            "description": "Waiting for Virginia SCC to process your filing (1-3 business days)"})

    # 8. EIN Issued
    ein = order.get("ein")
    ein_status = order.get("ein_status")
    if ein and reached(state, "ein_issued"):
        if ein_status == "provided_by_customer":
            desc = f"EIN {ein} — provided by you"
        else:
            desc = f"EIN {ein} issued by IRS{on_date(order.get('ein_issued_at'))}"
        steps.append({"key": "ein", "name": "EIN Issued", "status": "complete", "description": desc})
    elif order.get("ein_error"):
        steps.append({"key": "ein", "name": "EIN Issued", "status": "on_hold", "description": CUSTOMER_FRIENDLY_ERROR})
    elif ein_status == "queued" and order.get("next_available_window"):
        window = order["next_available_window"]
        if isinstance(window, str):
            window = datetime.datetime.fromisoformat(window)
        steps.append({"key": "ein", "name": "EIN Issued", "status": "current",
            "description": f"EIN application queued — will be submitted {format_eta(window)}"})
    elif state == "ein_requested":
        steps.append({"key": "ein", "name": "EIN Issued", "status": "current",
            "description": "EIN application submitted to IRS"})
    elif state == "awaiting_ein_filing":
        steps.append({"key": "ein", "name": "EIN Issued", "status": "current",
            "description": "Your SSN is on file — filing your EIN with the IRS shortly"})
    elif reached(state, "filing_confirmed"):
        steps.append({"key": "ein", "name": "EIN Issued", "status": "current",
            "description": "Preparing your EIN application..."})
    else:
        steps.append({"key": "ein", "name": "EIN Issued", "status": "pending",
            "description": "Will be applied for same day your LLC is approved (1-3 business days)"})

    # 9. Complete
    if state == "complete":
        steps.append({"key": "complete", "name": "Complete", "status": "complete",
            "description": "🎉 Your business is fully set up and ready to operate!"})
    else:
        steps.append({"key": "complete", "name": "Complete", "status": "pending",
            "description": "Almost there..."})

    return steps

def estimate_completion(order: dict, state: str) -> str:
    """Rough estimate, not a promise - SCC and the IRS move at their own
    pace and we say so elsewhere on the page. Just gives the customer a
    ballpark instead of nothing."""
    if state in ("complete", "name_rejected"):
        return None

    today = datetime.date.today()
    if order.get("skip_llc_formation"):
        days_out = 0 if reached(state, "ein_issued") else 1
    elif ORDER_STATE_INDEX[state] < ORDER_STATE_INDEX["name_selected"]:
        days_out = 5
    elif ORDER_STATE_INDEX[state] < ORDER_STATE_INDEX["filing_submitted"]:
        days_out = 4
    elif ORDER_STATE_INDEX[state] < ORDER_STATE_INDEX["filing_confirmed"]:
        days_out = 3
    elif ORDER_STATE_INDEX[state] < ORDER_STATE_INDEX["ein_issued"]:
        days_out = 1
    else:
        days_out = 0

    return (today + datetime.timedelta(days=days_out)).strftime("%B %-d, %Y")

security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Admin panel not configured - set ADMIN_PASSWORD in .env")
    if not secrets.compare_digest(credentials.password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Incorrect password", headers={"WWW-Authenticate": "Basic"})
    return True

def _wizard_context() -> dict:
    return {
        "stripe_publishable_key": STRIPE_PUBLISHABLE_KEY or "",
    }

VISITOR_SEEN_COOKIE = "lb_visitor_seen"
VISITOR_SEEN_COOKIE_MAX_AGE = 60 * 60 * 24 * 400  # ~400 days - browser cap on cookie lifetime

def track_page_view(date_str: str) -> None:
    """Increments today's page-view bucket plus a running all-time total -
    two separate documents (not one atomic transaction) since this is a
    rough visit counter for comparing app traffic against marketing-site
    traffic, not a financial ledger; losing perfect consistency between
    the two on a rare race is fine. Best-effort: a Firestore hiccup here
    must never break the homepage for a real visitor. Only called once
    per unique visitor per day - see home() - so this counts individual
    people, not raw hits (a refresh, back-button visit, or repeat load
    used to increment this on every single request)."""
    try:
        db.collection(PAGE_VIEWS_COLLECTION).document(date_str).set({"count": firestore.Increment(1)}, merge=True)
        db.collection(PAGE_VIEWS_COLLECTION).document("_total").set({"count": firestore.Increment(1)}, merge=True)
    except Exception as e:
        print(f"⚠️ Could not track page view: {e}")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # A visitor is only "new" the first time they hit the homepage on a
    # given calendar day - the cookie's value is just the last date they
    # were counted, so a second load today is a no-op but tomorrow counts
    # again, same as standard daily-unique-visitor analytics.
    today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    is_new_visit_today = request.cookies.get(VISITOR_SEEN_COOKIE) != today_str
    if is_new_visit_today:
        track_page_view(today_str)
    response = templates.TemplateResponse(request, "index.html", {
        "cancelled": request.query_params.get("cancelled") == "1",
        **_wizard_context(),
    })
    if is_new_visit_today:
        response.set_cookie(
            key=VISITOR_SEEN_COOKIE, value=today_str,
            max_age=VISITOR_SEEN_COOKIE_MAX_AGE, httponly=True, secure=True, samesite="lax",
            path="/",
        )
    return response

@app.get("/examples", response_class=HTMLResponse)
async def examples_page(request: Request):
    return templates.TemplateResponse(request, "examples.html", {})

@app.get("/examples/demo-site")
async def examples_demo_site():
    return FileResponse("app/static/examples/demo_site.html", media_type="text/html")

# RFC 9116. Only covers this app (app.launchbridge.ai / the run.app URL) -
# the marketing root (launchbridge.ai) is a separate Netlify-hosted site
# with no repo here, so it needs its own copy added independently.
_SECURITY_TXT = """Contact: mailto:support@launchbridge.ai
Expires: 2027-07-10T00:00:00.000Z
Preferred-Languages: en
Canonical: https://app.launchbridge.ai/.well-known/security.txt
# No bug bounty program at this time. Please report vulnerabilities
# responsibly and allow reasonable time for a fix before public disclosure.
"""

@app.get("/.well-known/security.txt")
async def security_txt():
    return Response(content=_SECURITY_TXT, media_type="text/plain")

@app.get("/virginia-llc/contractors", response_class=HTMLResponse)
async def landing_contractors(request: Request):
    return templates.TemplateResponse(request, "virginia_llc_contractors.html", {**_wizard_context()})

@app.get("/virginia-llc/done-for-you", response_class=HTMLResponse)
async def landing_done_for_you(request: Request):
    return templates.TemplateResponse(request, "virginia_llc_done_for_you.html", {**_wizard_context()})

@app.get("/virginia-llc/pricing", response_class=HTMLResponse)
async def landing_pricing(request: Request):
    return templates.TemplateResponse(request, "virginia_llc_pricing.html", {**_wizard_context()})

@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html")

@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse(request, "terms.html")

@app.post("/api/capture-lead")
async def api_capture_lead(request: Request):
    """Stores Step 1 / Step 2 wizard data for abandoned cart recovery.
    No auth required — anyone can create/update a lead. Inputs are
    length-capped and email-validated before writing to Firestore."""
    try:
        data = await request.json()
    except Exception:
        return {"ok": False}
    step = data.get("step", "step1")
    lead_id = (data.get("lead_id") or "").strip()
    update: dict = {}
    if step == "step1":
        update = {
            "business_idea": (data.get("business_idea") or "")[:500],
            "desired_name": (data.get("desired_name") or "")[:100],
            "landing_page": (data.get("landing_page") or "home")[:50],
            "step1_at": firestore.SERVER_TIMESTAMP,
            "converted": False,
        }
    elif step == "step2":
        email = (data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return {"ok": False}
        update = {
            "email": email,
            "first_name": (data.get("first_name") or "")[:50],
            "last_name": (data.get("last_name") or "")[:50],
            "phone": (data.get("phone") or "")[:20],
            "step2_at": firestore.SERVER_TIMESTAMP,
        }
    if not update:
        return {"ok": False}
    # leads is a shared collection (not split by ORDERS_COLLECTION like
    # orders is) - tag every write so staging traffic can be told apart
    # from real leads.
    update["app_env"] = APP_ENV
    if lead_id:
        ref = LEADS.document(lead_id)
        ref.set(update, merge=True)
    else:
        ref = LEADS.document()
        if "converted" not in update:
            update["converted"] = False
        ref.set(update)
        lead_id = ref.id
    return {"ok": True, "lead_id": lead_id}


# Note: name checking is now handled in /dashboard/orders/{id}/name (Step 3)

def parse_step4_details(form: dict) -> dict:
    """Pulls derived fields out of Step 4 (personal info): full_name and the
    principal_address string used throughout filing/EIN/website generation.
    sig_first/middle/last default to the owner's own name here (single-member
    LLC) - Step 5 lets the customer override them for a different primary
    signer or add additional members."""
    first_name = form.get("first_name", "")
    middle_name = form.get("middle_name", "")
    last_name = form.get("last_name", "")
    address = form.get("address", "")
    city = form.get("city", "")
    zipcode = form.get("zipcode", "")

    full_name = f"{first_name} {middle_name} {last_name}".replace("  ", " ").strip()
    principal_address = f"{address}, {city}, VA {zipcode}" if address else ""

    return {
        "full_name": full_name,
        "principal_address": principal_address,
        "sig_first": form.get("sig_first", first_name),
        "sig_middle": form.get("sig_middle", middle_name),
        "sig_last": form.get("sig_last", last_name),
    }


def parse_post_payment_intake(form: dict) -> dict:
    """Derives the LLC member signature list from Step 5 (business details).
    Address/full_name are Step 4's job (parse_step4_details above) - already
    on the order by the time Step 5 submits."""
    sig_first = form.get("sig_first", "")
    sig_middle = form.get("sig_middle", "")
    sig_last = form.get("sig_last", "")

    primary_sig = f"{sig_first} {sig_middle} {sig_last}".replace("  ", " ").strip()

    additional_members = []
    i = 2
    while True:
        first = form.get(f"extra_sig_first_{i}")
        if not first:
            break
        middle = form.get(f"extra_sig_middle_{i}", "")
        last = form.get(f"extra_sig_last_{i}", "")
        additional_members.append(f"{first} {middle} {last}".replace("  ", " ").strip())
        i += 1

    all_signatures = [primary_sig] + additional_members
    return {
        "primary_sig": primary_sig,
        "all_signatures": all_signatures,
    }

def run_name_check(order_id: str):
    """Defensive/retry-only path (see /admin/{id}/retry-agents) - the main
    flow now clears the name synchronously in the Step 3 dashboard page
    (POST /dashboard/orders/{id}/name) before the order ever reaches this
    function. Kept around to re-verify a name on demand. Advances
    paid -> name_selected if the business name is available on Virginia SCC;
    otherwise leaves the order at "paid" with the check result stored for
    the admin to see and act on (the automated pipeline doesn't know how to
    fix a taken name).

    A status of "UNAVAILABLE" means there was no local Chrome to drive the
    real check (e.g. this ran on Cloud Run, not the machine with CDP
    access) - not a real answer either way, so it advances anyway rather
    than stranding the order at "paid" forever. The real, authoritative
    check still happens for real inside file_llc_on_scc's own Step 3 right
    before filing, wherever that does have CDP access - if the name
    actually turns out to be taken, that step fails cleanly with a
    filing_error instead of silently completing."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return

    try:
        result = check_name_on_scc(order["business_name"])
        order_ref.set({"name_check": result}, merge=True)
        # UNAVAILABLE/UNKNOWN = couldn't determine definitively; advance the order
        # so it isn't stranded, and flag it for manual admin verification.
        auto_clear = result.get("available") or result.get("status") in ("UNAVAILABLE", "UNKNOWN")
        if auto_clear:
            record_state(order_ref, "name_selected", name_selected_at=firestore.SERVER_TIMESTAMP)
            run_document_generation(order_id)
        else:
            print(f"⚠️ Name check did not clear for order {order_id}: {result.get('message')}")
    except Exception as e:
        print(f"⚠️ Name check crashed for order {order_id}: {e}")
        order_ref.set({"name_check_error": str(e)}, merge=True)

# Mirrors dashboard_business.html's industry_code <select> options - the
# order only ever stores the numeric code, but the marketing plan agent
# needs a readable label to write anything industry-specific.
INDUSTRY_CODE_LABELS = {
    "0": "General Business",
    "35": "Insurance Agency",
    "36": "Mortgage Company",
    "65": "Church / Religious Organization",
}

def run_document_generation(order_id: str):
    """Generates the brand kit, marketing plan, reference name ideas, and
    signed LLC PDF as soon as the business name is confirmed (or
    immediately for skip_llc_formation orders) - none of these depend on
    SCC filing or the EIN, so customers get them well before those slower,
    externally-gated steps finish.

    Each agent call is isolated in its own try/except and skipped
    entirely if it already has a result on file - one Gemini/GitHub
    failure no longer blocks the others, and a retry (see
    /admin/{order_id}/retry-agents) only redoes whatever didn't already
    succeed instead of re-running everything from scratch."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return
    ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))

    business_name = order.get("business_name", "")
    full_name = order.get("full_name", "")
    # business_purpose may not exist on new orders that only filled 5 fields
    # pre-payment - fall back to business_idea (same intent, slightly different
    # phrasing) so the LLC PDF and agents have something useful to work with.
    business_purpose = order.get("business_purpose") or order.get("business_idea", "")
    principal_address = order.get("principal_address", "")
    primary_sig = order.get("primary_sig", "")
    business_idea = order.get("business_idea", "")
    target_customer = order.get("target_customer", "")

    update = {}
    errors = {}

    if not order.get("name_result"):
        try:
            update["name_result"] = screen_business_name(business_idea)
        except Exception as e:
            print(f"⚠️ Name screening agent failed for order {order_id}: {e}")
            errors["name_result"] = f"Name screening: {e}"

    existing_documents = order.get("documents") or {}

    if not order.get("brand_result"):
        try:
            brand_result = generate_brand_kit(
                business_name, business_idea, target_customer,
                full_name=full_name, email=order.get("email", ""), phone=order.get("phone", ""),
                website_url=order.get("website_url", ""),
            )
            pdf_bytes = brand_result.pop("pdf_bytes")
            logo_svg = brand_result.get("logo_svg", "")
            update["brand_result"] = brand_result
            object_name = upload_document(order_id, pdf_bytes, "application/pdf", "pdf")
            update["documents.brand_kit"] = {"object_name": object_name, "uploaded_at": firestore.SERVER_TIMESTAMP}
            if logo_svg:
                logo_object_name = upload_document(order_id, logo_svg.encode("utf-8"), "image/svg+xml", "svg")
                update["documents.logo"] = {"object_name": logo_object_name, "uploaded_at": firestore.SERVER_TIMESTAMP}
        except Exception as e:
            print(f"⚠️ Brand kit agent failed for order {order_id}: {e}")
            errors["brand_result"] = f"Brand kit: {e}"

    # Split into two independently-retryable steps rather than one try block -
    # marketing_plan_html previously got persisted to Firestore even when the
    # PDF build/upload that followed it in the same try failed, and since
    # this whole block is guarded by "if not order.get(marketing_plan_html)",
    # every later retry saw html already present and skipped straight past
    # the PDF forever, leaving the plan permanently viewable but never
    # downloadable. Persisting the raw plan dict (not just its rendered
    # HTML) means the PDF step can retry on its own without a second Gemini
    # call even if it only fails on this run.
    plan_data = order.get("marketing_plan_data")
    if not order.get("marketing_plan_html"):
        try:
            industry_label = INDUSTRY_CODE_LABELS.get(order.get("industry_code", "0"), "General Business")
            marketing_result = generate_marketing_plan(
                business_name, business_idea, target_customer, industry_label, location="Virginia")
            plan_data = marketing_result["plan"]
            update["marketing_plan_html"] = marketing_result["html"]
            update["marketing_plan_data"] = plan_data
        except Exception as e:
            print(f"⚠️ Marketing plan agent failed for order {order_id}: {e}")
            errors["marketing_result"] = f"Marketing plan: {e}"

    if plan_data and not existing_documents.get("marketing_plan"):
        try:
            pdf_bytes = build_marketing_plan_pdf(plan_data, business_name)
            object_name = upload_document(order_id, pdf_bytes, "application/pdf", "pdf")
            update["documents.marketing_plan"] = {"object_name": object_name, "uploaded_at": firestore.SERVER_TIMESTAMP}
        except Exception as e:
            print(f"⚠️ Marketing plan PDF failed for order {order_id}: {e}")
            errors["marketing_plan_pdf"] = f"Marketing plan PDF: {e}"

    # LLC PDF requires a real address and signature — skip until the customer
    # completes the post-payment intake form in the dashboard.
    if not existing_documents.get("articles") and not order.get("awaiting_intake"):
        try:
            pdf_bytes = generate_llc_pdf(business_name, full_name, business_purpose, full_name, principal_address, primary_sig)
            object_name = upload_document(order_id, pdf_bytes, "application/pdf", "pdf")
            doc_info = {"object_name": object_name, "uploaded_at": firestore.SERVER_TIMESTAMP}
            update["documents.articles"] = doc_info
            update["documents.operating_agreement"] = doc_info
        except Exception as e:
            print(f"⚠️ LLC PDF generation failed for order {order_id}: {e}")
            errors["pdf_filename"] = f"LLC PDF: {e}"

    # "documents_generated" means every agent has succeeded - this run's
    # results plus whatever already existed from an earlier run - not
    # just that this function executed, so both the status page and the
    # next retry have an honest signal instead of a permanent green
    # checkmark on a step that partially failed.
    have_all = all([
        update.get("name_result") or order.get("name_result"),
        update.get("brand_result") or order.get("brand_result"),
        update.get("marketing_plan_html") or order.get("marketing_plan_html"),
        update.get("documents.articles") or existing_documents.get("articles"),
    ])
    update["documents_generated"] = have_all
    if have_all:
        update["documents_generated_at"] = firestore.SERVER_TIMESTAMP
    update["documents_error"] = "; ".join(errors.values()) if errors else firestore.DELETE_FIELD
    # .update(), not .set(merge=True) - the dotted "documents.articles"-style
    # keys above are only treated as nested field paths (rather than literal
    # field names containing dots) by .update().
    order_ref.update(update)

def _persist_generated_logo(order_ref, result: dict) -> None:
    """generate_website only actually generates a logo the first time (see
    its own docstring) and reports that back via result["generated_logo"] -
    this saves it so every later call reads the same logo back in via
    order.get("logo_data_uri")/("favicon_data_uri") instead of generating a
    new one. A no-op on every regeneration after the first."""
    generated_logo = result.get("generated_logo")
    if generated_logo:
        order_ref.set(generated_logo, merge=True)

def run_early_assets(order_id: str):
    """Triggered immediately after Step 5 (business details) is submitted.
    The business name is already cleared by Step 3 at this point. Generates
    all deliverables that don't require SCC approval or an EIN: brand kit,
    marketing plan, LLC docs, and Stripe Connect account.

    The business website is deliberately NOT generated here - it's now
    admin-triggered (see run_website_generation / /admin/{order_id}/
    generate-website) with a review step before it goes live to the
    customer (run_website_approval). Keeping it out of this function is
    what lets brand kit/docs/marketing/Stripe Connect keep completing
    immediately after intake exactly as before, independent of whenever
    the admin gets to generating and approving the website.

    assets_status (fine-grained, drives admin error banners) tracks progress:
    "generating" → "complete" | "failed". record_state's ordinal
    assets_generating/assets_complete mirror the same milestones for
    reached()-based comparisons (timeline, poller) — assets_complete is only
    recorded on success so a failed run can be retried without violating
    forward-only ordinal semantics."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return
    ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))

    order_ref.set({"assets_status": "generating"}, merge=True)
    # Guarded by reached() - this can be re-triggered by /admin/{id}/retry-agents
    # on an order that has already advanced well past this milestone (e.g. to
    # retry just the website after filing/EIN already succeeded), and
    # record_state has no built-in ordinal check - an unconditional call here
    # would silently regress a later state backward.
    if not reached(order["state"], "assets_generating"):
        record_state(order_ref, "assets_generating")

    try:
        # Brand kit, marketing plan, LLC docs — idempotent, skips already-done steps
        run_document_generation(order_id)
        order = order_ref.get().to_dict()

        business_name = order.get("business_name", "")
        email = order.get("email", "")
        asset_error = None

        connect_account_id = order.get("stripe_connect_account_id")
        if not connect_account_id:
            try:
                account = create_connect_account(
                    email=email,
                    business_name=business_name,
                    multi_member=len(order.get("all_signatures", [])) > 1,
                )
                connect_account_id = account.id
                order_ref.set({"stripe_connect_account_id": connect_account_id}, merge=True)
            except Exception as e:
                print(f"⚠️ Could not create Stripe Connect for order {order_id}: {e}")
                asset_error = f"Could not set up your Stripe payment account: {e}"

        all_succeeded = not asset_error and bool(connect_account_id)
        order_ref.set({
            "early_assets_done": True,
            "early_assets_done_at": firestore.SERVER_TIMESTAMP,
            "assets_at": firestore.SERVER_TIMESTAMP if all_succeeded else None,
            "assets_status": "complete" if all_succeeded else "failed",
            "asset_generation_error": asset_error if asset_error else firestore.DELETE_FIELD,
        }, merge=True)

        # Only advance the ordinal state on success, and only if it hasn't
        # already moved further ahead (same retry-safety concern as above) -
        # a failed run leaves the state at intake_complete so a retry can
        # still reach assets_complete later without a backward jump.
        current_state = order_ref.get().to_dict().get("state", "intake_complete")
        if all_succeeded and not reached(current_state, "assets_complete"):
            record_state(order_ref, "assets_complete")

        order = {**order, "stripe_connect_account_id": connect_account_id}
        send_early_assets_email(order, order_id)
        send_admin_sms(f"🎨 Assets ready: {business_name}")
    except Exception as e:
        print(f"⚠️ run_early_assets crashed for order {order_id}: {e}")
        order_ref.set({
            "assets_status": "failed",
            "asset_generation_error": f"Asset generation crashed: {e}",
        }, merge=True)

SCC_FILED_STATES = {
    "filing_submitted", "filing_confirmed", "awaiting_ein_filing",
    "ein_requested", "ein_issued", "finalizing", "complete",
}

# An order is only eligible to have an EIN application filed once the SCC
# has actually confirmed the LLC (filing_confirmed) - we no longer fire EIN
# the moment paperwork is filed, since SCC approval can take days and the
# IRS step should reflect a real, confirmed entity. awaiting_ein_filing is
# the same eligibility window, just with the SSN already confirmed present
# too (see advance_past_filing_confirmed and notify_ein_ready) - kept as a
# separate state so the admin dashboard and status page can say "ready to
# file" instead of the more generic "approved, preparing your EIN".
EIN_ELIGIBLE_STATES = {"filing_confirmed", "awaiting_ein_filing"}

def advance_past_filing_confirmed(order_ref, order) -> bool:
    """Called whenever an order reaches filing_confirmed - whether by real
    SCC approval or because the customer already had an LLC and skipped
    formation. If the customer also already has an EIN (skip_ein), there's
    nothing left to wait on - jump straight to ein_issued with their
    provided EIN instead of queuing on IRS hours. Returns True if the
    caller should trigger run_asset_generation as a background task.

    Idempotent: a no-op if the order has already passed filing_confirmed.
    The hourly name-search poller (app/check_scc_status.py) and the
    admin's manual "Mark Approved by SCC" fallback can both notice the
    same real-world SCC approval and both call this. Without this guard
    the loser would re-send the approved/EIN-issued email and re-run
    asset generation a second time."""
    if reached(order.get("state", "draft"), "filing_confirmed"):
        return False

    if order.get("skip_ein"):
        existing_ein = order.get("existing_ein", "")
        record_state(order_ref, "ein_issued",
            ein=existing_ein,
            ein_status="provided_by_customer",
            filing_confirmed_at=firestore.SERVER_TIMESTAMP,
            ein_issued_at=firestore.SERVER_TIMESTAMP,
            filing_error=firestore.DELETE_FIELD,
            ein_error=firestore.DELETE_FIELD,
            asset_generation_error=firestore.DELETE_FIELD,
        )
        send_ein_issued_email(order, order_ref.id, existing_ein)
        send_admin_sms(f"✅ EIN done! {order.get('business_name', '')} {existing_ein}")
        return True

    extra = {"filing_confirmed_at": firestore.SERVER_TIMESTAMP, "filing_error": firestore.DELETE_FIELD}
    if is_irs_open():
        extra["ein_status"] = firestore.DELETE_FIELD
        extra["next_available_window"] = firestore.DELETE_FIELD
    else:
        window = next_irs_open()
        extra["ein_status"] = "queued"
        extra["next_available_window"] = window.isoformat()

    # The SSN is normally collected right after payment (see /collect-ssn),
    # long before SCC approval comes back - so by the time we get here it's
    # usually already sitting in the vault. When it is, this order has both
    # prerequisites for EIN filing and gets the more specific
    # awaiting_ein_filing state plus the admin SMS/email - rather than the
    # generic filing_confirmed, which still means "something is missing."
    order_id = order_ref.id
    ssn_ready = is_ssn_stored(order_id)
    new_state = "awaiting_ein_filing" if ssn_ready else "filing_confirmed"
    record_state(order_ref, new_state, **extra)
    if ssn_ready:
        notify_ein_ready(order, order_id)
    return False

def handle_name_rejected(order_ref, order: dict, order_id: str, message: str = "") -> None:
    """Shared reaction to a confirmed name rejection - called from both the
    admin's synchronous pre-flight check (POST /admin/{id}/verify-and-
    approve) and run_scc_filing's NameTakenError handler below, so a
    rejection caught either early (before the filing wizard is touched) or
    mid-wizard (SCC's own Step 3 distinguishability check) gets identical
    treatment: state moves backward to name_rejected (see ORDER_STATES'
    comment on why that's safe), the customer is emailed to pick a new
    name, and the admin is paged so nobody's left wondering why an order
    stalled."""
    business_name = order.get("business_name", "this name")
    record_state(order_ref, "name_rejected",
        name_rejected_at=firestore.SERVER_TIMESTAMP,
        name_rejected_reason=message or f'"{business_name}" is taken in Virginia.',
        filing_error=firestore.DELETE_FIELD,
    )
    send_name_rejected_email(order, order_id, business_name)
    send_admin_sms(f"⚠️ Name taken for {order_id} - customer needs to pick new name ({business_name})")

def run_scc_filing(order_id: str):
    """Triggered by the admin's Approve button. Files the LLC with SCC.
    EIN filing is a fully separate, later step (see run_ein_filing) that
    only becomes available once the admin marks the order filing_confirmed
    - SCC's real approval - and only during IRS business hours.

    Re-entrant: if the order already shows filing_submitted or later (e.g.
    the admin re-clicks Approve by mistake), the SCC step is skipped
    entirely rather than re-filing with SCC a second time.

    Wrapped in try/except: if the filer crashes outright (closed tab, site
    change, network blip) rather than returning False, the order must not
    be left stuck with no visible explanation."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return
    ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))

    if order.get("state") in SCC_FILED_STATES:
        return

    try:
        llc_customer_data = {
            "business_name": order["business_name"],
            "first_name": order["first_name"],
            "middle_name": order.get("middle_name", ""),
            "last_name": order["last_name"],
            "email": order["email"],
            "phone": order["phone"],
            "address": order["address"],
            "city": order["city"],
            "zipcode": order["zipcode"],
            "industry_code": order.get("industry_code", "0"),
            "duration": order.get("duration", "Perpetual"),
            "registered_agent_choice": order.get("registered_agent_choice", "self"),
        }
        # state_filing wraps just the real filing call, not this whole
        # function - run_scc_filing has its own early-return skip-guard
        # above (already-filed orders) that must never look like a step ran.
        with ai_ops.step(
            "state_filing", operation="file", autonomy="human_assisted", actor="agent",
            step_label="Virginia SCC LLC Filing",
            input_summary="Virginia SCC Articles of Organization filing",
        ) as s:
            llc_filed = file_llc_on_scc(llc_customer_data, interactive=False)
            s.output_summary = "LLC filed with Virginia SCC" if llc_filed else "SCC filing did not complete"
        if not llc_filed:
            order_ref.set({"filing_error": "SCC filing did not complete - check server screenshots, then re-approve to retry."}, merge=True)
            return

        record_state(order_ref, "filing_submitted",
            filing_submitted_at=firestore.SERVER_TIMESTAMP,
            filing_error=firestore.DELETE_FIELD,
        )
        send_llc_filed_email(order, order_id)
    except NameTakenError as e:
        print(f"🚫 Name confirmed taken for order {order_id}: {e}")
        handle_name_rejected(order_ref, order, order_id, message=str(e))
    except Exception as e:
        print(f"⚠️ SCC filing crashed for order {order_id}: {e}")
        order_ref.set({"filing_error": f"Filing crashed unexpectedly: {e}. Check server logs/screenshots."}, merge=True)
        send_admin_sms(f"⚠️ SCC crashed for {order.get('business_name', '')} - check admin")

def mark_ein_issued(order_ref, order: dict, order_id: str, ein: str, background_tasks: BackgroundTasks = None):
    """The standard finish line for every order the moment a real EIN is
    known - whether read back automatically (run_ein_filing) or typed in
    by the admin as a manual fallback (the /admin/{id}/mark-ein route).
    Records ein_issued, emails the customer their EIN, alerts the admin,
    and immediately kicks off website generation + Stripe Connect (see
    run_asset_generation) - there is no separate "Bryan" path, this is
    what every order does once it has an EIN."""
    record_state(order_ref, "ein_issued",
        ein=ein, ein_issued_at=firestore.SERVER_TIMESTAMP,
        ein_error=firestore.DELETE_FIELD, asset_generation_error=firestore.DELETE_FIELD,
    )
    order = {**order, "ein": ein}
    send_ein_issued_email(order, order_id, ein)
    send_admin_sms(f"✅ EIN done! {order.get('business_name', '')} {ein} - website generating now")
    if background_tasks is not None:
        background_tasks.add_task(run_asset_generation, order_id)
    else:
        run_asset_generation(order_id)

def run_ein_filing(order_id: str):
    """Triggered by the admin's Apply for EIN button (dashboard, or the
    one-click email link from notify_ein_ready) - only reachable once the
    order is filing_confirmed/awaiting_ein_filing (SCC's real approval).
    Re-checks IRS business hours (Mon-Fri 7am-10pm ET) itself, in addition
    to the route's own check, to cover the race where hours close between
    the click and this background task actually running.

    Fully automatic, per explicit instruction: file_ein_with_irs runs with
    interactive=False, so it clicks Submit itself the moment the form is
    filled - there is no human "are you sure" checkpoint before a real,
    permanent EIN is issued. on_submitted fires the instant that happens
    (durably flips ein_submitted_to_irs and deletes the now-used SSN)
    independent of whatever succeeds or crashes afterward while reading
    the confirmation page back - so a crash mid-scrape can never look like
    "never filed" to the guard below, which would otherwise risk filing a
    second, duplicate EIN for the same business."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return
    ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))

    if order.get("ein_submitted_to_irs"):
        order_ref.set({
            "ein_error": "An EIN was already submitted to the IRS for this order, but the number was not "
                          "recorded automatically. Check /tmp/ein_confirmation.png on the server (or the admin "
                          "notification email) for the EIN, then enter it via Mark EIN Issued. Do NOT click "
                          "Apply for EIN again - that would file a second, duplicate EIN."
        }, merge=True)
        send_admin_sms(f"⚠️ EIN already submitted for {order.get('business_name', '')} - enter manually, do not re-file")
        return

    if not is_irs_open():
        # Not a failure, just timing - keep it out of ein_error so neither
        # dashboard shows a red box for something that isn't actually wrong.
        window = next_irs_open()
        order_ref.set({
            "ein_status": "queued",
            "next_available_window": window.isoformat(),
            "ein_error": firestore.DELETE_FIELD,
        }, merge=True)
        return

    def on_submitted():
        order_ref.set({"ein_submitted_to_irs": True, "ein_submitted_at": firestore.SERVER_TIMESTAMP}, merge=True)
        delete_ssn(order_id)

    try:
        ssn = decrypt_ssn(order_id)
        if not ssn:
            order_ref.set({
                "ein_error": "SSN is no longer stored (it was deleted after 72 hours, or EIN was already filed once). The customer must re-enter it before EIN filing can proceed."
            }, merge=True)
            send_admin_sms(f"⚠️ EIN setup failed for {order.get('business_name', '')} - SSN missing, check admin")
            return

        today = datetime.date.today()
        ein_customer_data = {
            "business_name": order["business_name"],
            "first_name": order["first_name"],
            "middle_name": order.get("middle_name", ""),
            "last_name": order["last_name"],
            "ssn": ssn,
            "address": order["address"],
            "city": order["city"],
            "state": "VA",
            "zipcode": order["zipcode"],
            "phone": order["phone"],
            "county": order["county"],
            "start_month": today.strftime("%B"),
            "start_year": str(today.year),
            "members": str(len(order.get("all_signatures", []))),
            "business_description": order["business_purpose"],
        }
        # ein_customer_data (built just above) contains the customer's SSN -
        # input_summary here must be a fixed string, never derived from it.
        with ai_ops.step(
            "ein_preparation", operation="file", autonomy="autonomous", actor="agent",
            step_label="EIN (SS-4) Filing with IRS",
            input_summary="EIN application data assembled for IRS submission",
        ) as s:
            result = file_ein_with_irs(ein_customer_data, interactive=False, on_submitted=on_submitted)
            s.output_summary = "EIN issued" if result.get("success") else "EIN application not completed"

        cp575_bytes = result.get("cp575_bytes")
        if cp575_bytes:
            try:
                object_name = upload_document(order_id, cp575_bytes, "application/pdf", "pdf")
                order_ref.update({"documents.ein_letter": {"object_name": object_name, "uploaded_at": firestore.SERVER_TIMESTAMP}})
            except Exception as e:
                print(f"⚠️ Could not upload CP575 letter for order {order_id}: {e}")

        if result["success"]:
            mark_ein_issued(order_ref, order, order_id, result["ein"])
            return

        if result.get("rejected"):
            order_ref.set({"ein_error": "The IRS could not verify the responsible party's SSN. Please contact support@launchbridge.ai."}, merge=True)
            send_admin_sms(f"⚠️ EIN rejected by IRS for {order.get('business_name', '')} - SSN mismatch, check admin")
            return

        if result.get("submitted"):
            # ein_submitted_to_irs (set by on_submitted, above) already
            # guards this order against ever being re-filed - this just
            # surfaces the number however an admin can recover it.
            order_ref.set({
                "ein_error": f"EIN was submitted to the IRS but could not be read from the confirmation page. "
                              f"Check {result.get('screenshot')} on the server, then enter it via Mark EIN Issued."
            }, merge=True)
            send_admin_sms(f"⚠️ EIN filed for {order.get('business_name', '')} but number unreadable - check screenshot, enter manually")
            return

        # Not submitted at all (e.g. couldn't find the Submit button) -
        # on_submitted never fired, so this is safe to retry.
        order_ref.set({"ein_error": result.get("error", "EIN filing did not complete - check server screenshots, then apply again to retry.")}, merge=True)
        send_admin_sms(f"⚠️ EIN setup failed for {order.get('business_name', '')} - check admin")
    except Exception as e:
        # Scrubbed defensively, in case a Playwright/IRS error message
        # ever happens to echo back the SSN it was just given.
        safe_error = scrub_ssn(str(e))
        print(f"⚠️ EIN filing crashed for order {order_id}: {safe_error}")
        order_ref.set({"ein_error": f"Filing crashed unexpectedly: {safe_error}. Check server logs/screenshots."}, merge=True)
        send_admin_sms(f"⚠️ EIN setup failed for {order.get('business_name', '')} - check admin")

async def ein_queue_scheduler():
    """Runs for the lifetime of the process, woken every 5 minutes. Picks
    up any order stuck at filing_confirmed/awaiting_ein_filing with
    ein_status="queued" (set either by mark-filed or a blocked Apply for
    EIN click) and fires the EIN filing the moment IRS hours actually open
    - so a queued order doesn't just sit there until an admin happens to
    click again. run_ein_filing's own ein_submitted_to_irs guard makes
    this safe even if a queued order somehow got submitted by another
    path in the meantime."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            if is_irs_open():
                for eligible_state in EIN_ELIGIBLE_STATES:
                    query = (
                        ORDERS
                        .where("state", "==", eligible_state)
                        .where("ein_status", "==", "queued")
                    )
                    for doc in query.stream():
                        print(f"⏰ IRS hours open - auto-submitting queued EIN for order {doc.id}")
                        future = loop.run_in_executor(None, run_ein_filing, doc.id)
                        future.add_done_callback(
                            lambda f, oid=doc.id: print(f"⚠️ Auto EIN submit crashed for {oid}: {f.exception()}")
                            if f.exception() else None
                        )
        except Exception as e:
            print(f"⚠️ EIN queue scheduler tick failed: {e}")
        await asyncio.sleep(300)

SSN_EXPIRY_HOURS = 72

async def ssn_expiry_scheduler():
    """Runs for the lifetime of the process, woken every hour. An SSN
    that's been sitting encrypted in Firestore for more than 72 hours
    without EIN filing having actually started yet gets deleted outright
    - same "don't hold onto it longer than necessary" rationale that kept
    it in-memory-only in the first place, just extended to cover
    "persisted too long" instead of "persisted at all". Scans every order
    rather than a filtered query - order volume here is small enough that
    a full scan once an hour is simpler to ship than a composite index."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            for doc in ORDERS.stream():
                order = doc.to_dict()
                if not order.get("ssn_encrypted") or reached(order.get("state", "draft"), "ein_requested"):
                    continue
                age = await loop.run_in_executor(None, ssn_age_hours, doc.id)
                if age < SSN_EXPIRY_HOURS:
                    continue
                await loop.run_in_executor(None, delete_ssn, doc.id)
                doc.reference.set({"ssn_expired": True}, merge=True)
                send_ssn_expired_email(order, doc.id)
                print(f"🗑️ SSN expired (age {age:.1f}h) and deleted for order {doc.id}")
        except Exception as e:
            print(f"⚠️ SSN expiry sweep tick failed: {e}")
        await asyncio.sleep(3600)

async def stripe_activation_scheduler():
    """Runs every hour. check_and_update_website already handles the fast
    path (payment_button_live already set) as a cheap no-op, so this scans
    every order rather than needing a composite Firestore index for
    "has a Stripe account, isn't live yet, and has reached ein_issued" -
    same tradeoff ssn_expiry_scheduler makes, and order volume is small
    enough that it's not worth provisioning one.

    This is the backstop, not the primary path: a customer who reopens
    their dashboard after finishing Stripe onboarding gets picked up
    immediately by ensure_payment_link + the admin's manual "Check Stripe
    Status" button - this just catches anyone who doesn't."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            for doc in ORDERS.stream():
                order = doc.to_dict()
                if not order.get("stripe_connect_account_id") or order.get("payment_button_live"):
                    continue
                if not reached(order.get("state", "draft"), "ein_issued"):
                    continue
                result = await loop.run_in_executor(None, check_and_update_website, doc.id)
                if result.get("updated"):
                    print(f"💳 Payment button activated for order {doc.id}: {result.get('url')}")
        except Exception as e:
            print(f"⚠️ Stripe activation sweep tick failed: {e}")
        await asyncio.sleep(3600)

async def abandoned_cart_scheduler():
    """Runs every 30 minutes. Two separate recovery paths:

    1. Pre-payment leads (LEADS, keyed by lead_id from /api/capture-lead) -
       recovery emails at 1h and 24h after step1_at. Marks converted=True
       once a paid order exists for that email so we never send to a
       paying customer. These leads usually have no email at all now (Step
       1 only collects the idea - see index.html) unless the customer
       reached old-style Step 2 fields, so this loop is mostly a no-op
       going forward but still fires for anyone who does have one on file.

    2. Paid orders stuck mid-wizard (Steps 3-5) - unlike leads, we already
       know these people paid and have a real email, so a single 1h
       recovery email with a magic link straight back to their next
       incomplete step (next_incomplete_step_url) is enough; no 24h
       follow-up since by then it's better handled as a support case."""
    await asyncio.sleep(60)  # brief startup delay
    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            one_hour_ago = now - datetime.timedelta(hours=1)
            twenty_four_hours_ago = now - datetime.timedelta(hours=24)
            for doc in LEADS.where("converted", "==", False).stream():
                lead = doc.to_dict()
                lead_id = doc.id
                email = lead.get("email", "").strip()
                if not email:
                    continue
                # Detect conversion: any non-draft order for this email
                paid = next(
                    (d for d in ORDERS.where("email", "==", email).limit(5).stream()
                     if d.to_dict().get("state", "draft") != "draft"),
                    None,
                )
                if paid:
                    LEADS.document(lead_id).set({"converted": True}, merge=True)
                    continue
                step1_at = lead.get("step1_at")
                if not step1_at:
                    continue
                if not lead.get("recovery_1h_sent") and step1_at <= one_hour_ago:
                    send_abandoned_cart_email_1h(lead)
                    LEADS.document(lead_id).set({"recovery_1h_sent": True}, merge=True)
                if not lead.get("recovery_24h_sent") and step1_at <= twenty_four_hours_ago:
                    send_abandoned_cart_email_24h(lead)
                    LEADS.document(lead_id).set({"recovery_24h_sent": True}, merge=True)

            for doc in ORDERS.where("state", "in", ["paid", "name_selected"]).stream():
                order = doc.to_dict()
                if order.get("mid_flow_recovery_sent") or not order.get("email"):
                    continue
                last_touch = order.get("name_selected_at") or order.get("paid_at")
                if not last_touch or last_touch > one_hour_ago:
                    continue
                step_url = next_incomplete_step_url(doc.id, order)
                if not step_url:
                    continue  # finished the wizard between the query and here
                send_mid_flow_recovery_email(order, step_url)
                doc.reference.set({"mid_flow_recovery_sent": True}, merge=True)
        except Exception as e:
            print(f"⚠️ abandoned_cart_scheduler error: {e}")
        await asyncio.sleep(1800)

SCC_HEALTH_CHECK_INTERVAL_SECONDS = 86400  # once a day

async def scc_health_check_scheduler():
    """Runs immediately on startup (so a bad deploy surfaces right away
    instead of waiting up to 24h) and then once a day. Monitoring only -
    doesn't touch check_name_public/check_name_on_scc's own cache, retries,
    or Chrome fallback, and never affects a real customer's name check.

    Pure httpx/BeautifulSoup work like check_name_public itself, so this is
    safe to run everywhere check_name_public runs, Cloud Run included."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            result = await loop.run_in_executor(None, run_scc_health_check)
            if not result.get("ok"):
                send_admin_sms(
                    f"⚠️ SCC health check FAILED at step '{result.get('failed_step')}' "
                    f"- see Cloud Run logs for the raw response"
                )
        except Exception as e:
            print(f"⚠️ scc_health_check_scheduler tick failed unexpectedly: {e}")
            send_admin_sms(f"⚠️ SCC health check crashed unexpectedly: {e}")
        await asyncio.sleep(SCC_HEALTH_CHECK_INTERVAL_SECONDS)

def _maybe_finalize_order(order_id: str) -> None:
    """Advances an order the rest of the way to "complete" once its website
    is live (website_url set - see run_website_approval) AND the EIN/Stripe
    side of the pipeline already reached "finalizing" (see
    run_asset_generation). Website approval and EIN/Stripe finishing can
    happen in either order now that website generation is admin-triggered
    rather than automatic, so both run_asset_generation and
    run_website_approval call this at their own tail - whichever one
    finishes second is the one that actually flips the order to complete.
    No-op if the website isn't live yet, finalizing hasn't been reached
    yet, or the order is already complete - re-fetches fresh from
    Firestore rather than trusting a caller's possibly-stale dict, since
    it can run well after the caller loaded its copy of the order."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return
    ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))
    website_url = order.get("website_url")
    state = order.get("state", "draft")
    if not website_url or state == "complete" or not reached(state, "finalizing"):
        return

    record_state(order_ref, "complete", fulfilled_at=firestore.SERVER_TIMESTAMP, complete_at=firestore.SERVER_TIMESTAMP)
    business_name = order.get("business_name", "")
    send_everything_complete_email(order, order_id)
    send_admin_sms(f"🎉 Done! {business_name} fully onboarded")
    ai_ops.emit_event(
        "delivery", "completed", step_label="Business Package Delivered",
        operation="assemble", autonomy="autonomous", actor="system", status="success",
        output_summary="LLC, EIN, brand kit, marketing plan, and website all delivered",
    )

def run_asset_generation(order_id: str):
    """Triggered once the admin records the real EIN (or immediately for
    skip_ein orders). Documents (brand kit, marketing plan, signed LLC PDF)
    are already done by now - see run_document_generation, which runs much
    earlier, right after name verification. This step just sets up the
    customer's Stripe Connect Standard account and a pay-what-you-want
    Payment Link on it.

    The website itself is never (re)generated here - see run_early_assets
    for why - so this only ever reads whatever website_url already exists
    (None if the admin hasn't generated/approved one yet); see
    _maybe_finalize_order for how "complete" eventually gets set once both
    this and the website approval have happened, in whichever order."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return
    ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))

    try:
        business_name = order["business_name"]
        email = order["email"]

        # Re-entrant by field, not by a single all-or-nothing flag - a
        # retry (see /admin/{order_id}/retry-agents) only redoes whichever
        # of these didn't already succeed, instead of recreating a second
        # Stripe Connect account.
        connect_account_id = order.get("stripe_connect_account_id")
        payment_link_url = order.get("stripe_payment_link_url")
        asset_error = None
        if not connect_account_id:
            try:
                account = create_connect_account(
                    email=email,
                    business_name=business_name,
                    multi_member=len(order.get("all_signatures", [])) > 1,
                )
                connect_account_id = account.id
            except Exception as e:
                print(f"⚠️ Could not set up Stripe Connect for order {order_id}: {e}")
                asset_error = f"Could not set up your Stripe payment account: {e}"

        # A payment link needs at least one payment method enabled on the
        # connected account, and Stripe doesn't enable any until the
        # customer finishes onboarding (TOS acceptance, bank details,
        # etc. - see create_account_link). A freshly created account
        # never has this yet, so this is expected to no-op right here;
        # status_page's ensure_payment_link fills it in lazily the next
        # time the status page is viewed, once is_account_active flips
        # true. Not an error - it's pending the customer's own action.
        if connect_account_id and not payment_link_url and is_account_active(connect_account_id):
            try:
                payment_link_url = create_pay_what_you_want_payment_link(connect_account_id, business_name)
            except Exception as e:
                print(f"⚠️ Could not create payment link for order {order_id}: {e}")
                asset_error = ((asset_error + " ") if asset_error else "") + f"Could not create your payment link: {e}"

        record_state(order_ref, "finalizing",
            website_url=order.get("website_url"),
            stripe_connect_account_id=connect_account_id,
            stripe_payment_link_url=payment_link_url,
            assets_generated_at=firestore.SERVER_TIMESTAMP,
            asset_generation_error=asset_error if asset_error else firestore.DELETE_FIELD,
        )
        _maybe_finalize_order(order_id)
    except Exception as e:
        print(f"⚠️ Asset generation crashed for order {order_id}: {e}")
        order_ref.set({"asset_generation_error": f"Asset generation crashed unexpectedly: {e}. Check server logs."}, merge=True)

def run_website_generation(order_id: str) -> dict:
    """Admin-triggered (see /admin/{order_id}/generate-website) - the only
    way a website is ever generated now, since run_early_assets/
    run_asset_generation no longer do it automatically. Doubles as the
    "Regenerate" action in the pre-approval review panel: safe to call
    repeatedly, each call just overwrites the previous draft.

    Deploys with live=False, so deploy_website/save_website_to_order write
    website_draft_url/website_draft_template/website_draft_content instead
    of the live website_* fields - the customer-facing dashboard and
    timeline only ever read website_url, so the draft is completely inert
    until an admin reviews it and calls run_website_approval. It's still
    deployed to the same real Firebase Hosting site_id the live site will
    eventually use (see make_site_id), so approval never needs a second
    deploy - it just copies the draft fields over.

    Returns {"success": True, "url": ...} or {"success": False, "error": ...}
    so the admin's synchronous button can show the real draft URL (or
    error) immediately."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return {"success": False, "error": "Order not found."}
    ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))

    try:
        business_name = order["business_name"]
        business_idea = order["business_idea"]
        target_customer = order["target_customer"]
        services = [
            {"name": order.get(f"service_{i}_name", ""), "description": order.get(f"service_{i}_desc", "")}
            for i in (1, 2, 3)
        ]
        photos = [order.get(f"photo_{i}_data") for i in (1, 2, 3)]
        _site_id = make_site_id(business_name, order_id)
        result = generate_website(
            business_name, business_idea, target_customer,
            template_name=order.get("website_template", "professional"),
            tagline=order.get("website_tagline", ""),
            description=order.get("website_description", ""),
            services=services,
            hours=order.get("business_hours", ""),
            photos=photos,
            instagram_url=order.get("instagram_url", ""),
            facebook_url=order.get("facebook_url", ""),
            tiktok_url=order.get("tiktok_url", ""),
            linkedin_url=order.get("linkedin_url", ""),
            color_preference=order.get("color_preference", "default"),
            custom_primary_color=order.get("custom_primary_color", ""),
            backdrop_image_choice=order.get("backdrop_image_choice", ""),
            payment_link_url=order.get("stripe_payment_link_url"),
            order_id=order_id,
            site_url=f"https://{_site_id}.web.app",
            show_contact=bool(order.get("website_contact_show")),
            contact_phone=order.get("website_contact_phone", ""),
            contact_email=order.get("website_contact_email", ""),
            contact_address=order.get("website_contact_address", ""),
            logo_data_uri=order.get("logo_data_uri"),
            favicon_data_uri=order.get("favicon_data_uri"),
        )
        deployed = deploy_website(business_name, result["html"], order_id=order_id, live=False)
        if deployed:
            order_ref.set({
                "website_draft_template": result["template"],
                "website_draft_content": result["content"],
                "website_review_status": "pending_review",
                "website_generation_attempts": firestore.Increment(1),
                "asset_generation_error": firestore.DELETE_FIELD,
            }, merge=True)
            _persist_generated_logo(order_ref, result)
            print(f"✅ Website draft generated for order {order_id}: {deployed['url']}")
            return {"success": True, "url": deployed["url"]}
        else:
            error = "Could not deploy your business website draft - check server logs, then retry."
            print(f"⚠️ Website draft deploy returned no URL for order {order_id} - check Firebase deploy logs above.")
            order_ref.set({"asset_generation_error": error}, merge=True)
            return {"success": False, "error": error}
    except Exception as e:
        error = f"Website generation crashed unexpectedly: {e}. Check server logs."
        print(f"⚠️ Website generation crashed for order {order_id}: {e}")
        order_ref.set({"asset_generation_error": error}, merge=True)
        return {"success": False, "error": error}

def run_website_approval(order_id: str) -> dict:
    """Admin-triggered (see /admin/{order_id}/approve-website) - the only
    place a generated website draft (see run_website_generation) actually
    becomes visible to the customer. Copies the reviewed draft fields over
    to the live website_* fields - the same fields the old fully-automatic
    pipeline used to set the moment generation finished, so every
    customer-facing surface that already keys off website_url (dashboard
    timeline, the "Share Your Business Launch" panel) lights up exactly as
    before, just on the admin's decision instead of automatically.

    website_live_email_sent guards send_website_live_email so re-approving
    after a later post-approval "Regenerate Website" never re-sends it -
    that email is about the site's first appearance to the customer, not
    every subsequent content change.

    Also opportunistically calls check_and_update_website - if the
    customer's Stripe account happened to finish onboarding before the
    website was approved, check_and_update_website would otherwise have
    skipped adding the payment button (see its own website_url guard) and
    waited for the next hourly scheduler run.

    Finally calls _maybe_finalize_order, covering the case where EIN/Stripe
    (run_asset_generation) already reached "finalizing" before this
    website was approved.

    Returns {"success": True, "url": ...} or {"success": False, "error": ...}."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return {"success": False, "error": "Order not found."}
    ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))

    draft_url = order.get("website_draft_url")
    if not draft_url:
        return {"success": False, "error": "No generated website draft to approve yet - click Generate Website first."}

    order_ref.set({
        "website_url": draft_url,
        "website_site_id": order.get("website_draft_site_id"),
        "website_template": order.get("website_draft_template"),
        "website_content": order.get("website_draft_content"),
        "website_review_status": "approved",
        "website_approved_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)
    order["website_url"] = draft_url

    ai_ops.emit_event(
        "human_review", "completed", step_label="Website Approve & Publish",
        operation="review", autonomy="human_only", actor="human", status="success",
        output_summary="admin approved and published the generated website draft",
        human_touched=True, human_action="approve",
    )

    if not order.get("website_live_email_sent"):
        send_website_live_email(order, order_id)
        order_ref.set({"website_live_email_sent": True}, merge=True)
        send_admin_sms(f"🌐 Site live! {order.get('business_name', '')}")

    try:
        check_and_update_website(order_id)
    except Exception as e:
        print(f"⚠️ check_and_update_website failed right after approving website for order {order_id}: {e}")

    _maybe_finalize_order(order_id)
    return {"success": True, "url": draft_url}

def run_website_regeneration(order_id: str) -> dict:
    """Admin-triggered (see /admin/{order_id}/regenerate-website) - for an
    already-APPROVED, already-live website only (order.website_url already
    set). Unlike run_website_generation, this always re-generates and
    re-deploys straight to the live site, no review step - the whole point
    is to redo a website the admin wasn't happy with after it was already
    shown to the customer. Deliberately only touches the website - Stripe
    Connect/payment link are untouched, so this never risks creating a
    second Connect account.

    Returns {"success": True, "url": ...} or {"success": False, "error": ...}
    so the admin's synchronous regenerate button can show a real result -
    the Firestore side effects below are unchanged and still happen either
    way, this return value is additive."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return {"success": False, "error": "Order not found."}
    ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))

    try:
        business_name = order["business_name"]
        business_idea = order["business_idea"]
        target_customer = order["target_customer"]
        services = [
            {"name": order.get(f"service_{i}_name", ""), "description": order.get(f"service_{i}_desc", "")}
            for i in (1, 2, 3)
        ]
        photos = [order.get(f"photo_{i}_data") for i in (1, 2, 3)]
        _site_id = make_site_id(business_name, order_id)
        result = generate_website(
            business_name, business_idea, target_customer,
            template_name=order.get("website_template", "professional"),
            tagline=order.get("website_tagline", ""),
            description=order.get("website_description", ""),
            services=services,
            hours=order.get("business_hours", ""),
            photos=photos,
            instagram_url=order.get("instagram_url", ""),
            facebook_url=order.get("facebook_url", ""),
            tiktok_url=order.get("tiktok_url", ""),
            linkedin_url=order.get("linkedin_url", ""),
            color_preference=order.get("color_preference", "default"),
            custom_primary_color=order.get("custom_primary_color", ""),
            backdrop_image_choice=order.get("backdrop_image_choice", ""),
            payment_link_url=order.get("stripe_payment_link_url"),
            order_id=order_id,
            site_url=f"https://{_site_id}.web.app",
            show_contact=bool(order.get("website_contact_show")),
            contact_phone=order.get("website_contact_phone", ""),
            contact_email=order.get("website_contact_email", ""),
            contact_address=order.get("website_contact_address", ""),
            logo_data_uri=order.get("logo_data_uri"),
            favicon_data_uri=order.get("favicon_data_uri"),
        )
        deployed = deploy_website(business_name, result["html"], order_id=order_id)
        if deployed:
            order_ref.set({
                "website_template": result["template"], "website_content": result["content"],
                "website_url": deployed["url"], "asset_generation_error": firestore.DELETE_FIELD,
            }, merge=True)
            _persist_generated_logo(order_ref, result)
            print(f"✅ Website regenerated for order {order_id}: {deployed['url']}")
            return {"success": True, "url": deployed["url"]}
        else:
            error = "Could not redeploy your business website - check server logs, then retry."
            print(f"⚠️ Website regeneration deploy returned no URL for order {order_id} - check Firebase deploy logs above.")
            order_ref.set({"asset_generation_error": error}, merge=True)
            return {"success": False, "error": error}
    except Exception as e:
        error = f"Website regeneration crashed unexpectedly: {e}. Check server logs."
        print(f"⚠️ Website regeneration crashed for order {order_id}: {e}")
        order_ref.set({"asset_generation_error": error}, merge=True)
        return {"success": False, "error": error}

def run_logo_regeneration(order_id: str) -> dict:
    """Admin-triggered (see /admin/{order_id}/regenerate-logo) - for an
    order whose brand_result predates the logo feature, or where the AI
    logo just didn't come out well. Deliberately does NOT call
    generate_website_content/get_image_keywords again (no Gemini calls for
    tagline/about/services/hero photo) - only regenerates the logo itself,
    then redeploys the already-generated website content with the new logo
    swapped in. Returns {"success": True, "url": ...} or
    {"success": False, "error": ...}, same shape as run_website_regeneration."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return {"success": False, "error": "Order not found."}

    try:
        business_name = order.get("business_name", "")
        content = order.get("website_content") or {}
        template_name = order.get("website_template", "professional")
        if content.get("primary_color") and content.get("secondary_color"):
            primary_color, secondary_color = content["primary_color"], content["secondary_color"]
        else:
            primary_color, secondary_color = TEMPLATE_DEFAULT_COLORS.get(template_name, TEMPLATE_DEFAULT_COLORS["professional"])
        industry_label = INDUSTRY_CODE_LABELS.get(order.get("industry_code", "0"), "General Business")

        logo_variations = generate_logo_variations(business_name, primary_color, secondary_color, industry_label)
        favicon_svg = generate_favicon_svg(business_name, primary_color)
        logo_update = {
            "logo_svg": logo_variations["horizontal"],
            "logo_variations": logo_variations,
            "logo_data_uri": svg_to_data_uri(logo_variations["horizontal"]),
            "logo_variations_data_uri": {k: svg_to_data_uri(v) for k, v in logo_variations.items()},
            "favicon_data_uri": svg_to_data_uri(favicon_svg),
        }
        order_ref.set(logo_update, merge=True)

        if not content or not order.get("website_url"):
            # No website deployed yet to redeploy - the new logo is saved
            # and will be picked up whenever the website is first generated.
            return {"success": True, "url": order.get("website_url", "")}

        _site_id = make_site_id(business_name, order_id)
        html = render_website_html(
            content, business_name, template_name,
            payment_link_url=order.get("stripe_payment_link_url"),
            hours=order.get("business_hours"),
            instagram_url=order.get("instagram_url"),
            facebook_url=order.get("facebook_url"),
            tiktok_url=order.get("tiktok_url"),
            linkedin_url=order.get("linkedin_url"),
            order_id=order_id,
            site_url=f"https://{_site_id}.web.app",
            contact_phone=order.get("website_contact_phone") if order.get("website_contact_show") else None,
            contact_email=order.get("website_contact_email") if order.get("website_contact_show") else None,
            contact_address=order.get("website_contact_address") if order.get("website_contact_show") else None,
            logo_data_uri=logo_update["logo_data_uri"],
            favicon_data_uri=logo_update["favicon_data_uri"],
        )
        deployed = deploy_website(business_name, html, order_id=order_id)
        if deployed:
            print(f"✅ Logo regenerated and redeployed for order {order_id}: {deployed['url']}")
            return {"success": True, "url": deployed["url"]}
        error = "Logo saved, but could not redeploy the website - check server logs, then retry."
        return {"success": False, "error": error}
    except Exception as e:
        error = f"Logo regeneration crashed unexpectedly: {e}. Check server logs."
        print(f"⚠️ Logo regeneration crashed for order {order_id}: {e}")
        return {"success": False, "error": error}

ATTRIBUTION_COOKIE_NAME = "lb_attribution"
ATTRIBUTION_STRING_FIELDS = ("gclid", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")

def parse_attribution_cookie(raw: str | None) -> dict:
    """Reads the marketing site's lb_attribution cookie (domain
    .launchbridge.ai) into the fields stored on the order at creation time.
    app.launchbridge.ai is a subdomain of launchbridge.ai, so the browser
    already sends this cookie on every request here without any extra
    wiring - this just parses what's on the request.

    Never raises and never blocks order creation: a visitor who came
    straight to the app (no cookie), an ad blocker that stripped it, or a
    malformed value all just mean every field comes back null, same as if
    they'd never been near a Google/UTM link at all."""
    fields = {"attribution_captured_at": None, **{k: None for k in ATTRIBUTION_STRING_FIELDS}}
    if not raw:
        return fields

    data = None
    for candidate in (raw, unquote(raw)):
        try:
            data = json.loads(candidate)
            break
        except (ValueError, TypeError):
            continue
    if not isinstance(data, dict):
        return fields

    for key in ATTRIBUTION_STRING_FIELDS:
        value = data.get(key)
        fields[key] = value if isinstance(value, str) and value else None

    # The cookie's own capture time, not order-creation time - a visitor can
    # click a Google ad and not actually check out until days later, and the
    # gap itself is signal (see how utm/gclid data feeds ad spend attribution).
    captured_raw = data.get("timestamp") or data.get("captured_at") or data.get("attribution_captured_at")
    if isinstance(captured_raw, str):
        try:
            fields["attribution_captured_at"] = datetime.datetime.fromisoformat(captured_raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    elif isinstance(captured_raw, (int, float)):
        try:
            seconds = captured_raw / 1000 if captured_raw > 1e12 else captured_raw
            fields["attribution_captured_at"] = datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
        except (ValueError, OSError, OverflowError):
            pass
    return fields

def _classify_category_fields(business_idea: str) -> dict:
    """Runs the AI business-category classifier and shapes its result into
    the fields stored on the order doc. Returns {} for a blank idea rather
    than storing an "Other / Not sure" placeholder before there's any text
    to classify - the Step 5 confirmation UI only shows once category is
    actually set. category_source starts as "ai" and only becomes
    "confirmed" once the customer reviews it in Step 5 (see
    dashboard_category_confirm)."""
    business_idea = (business_idea or "").strip()
    if not business_idea:
        return {}
    result = classify_business_category(business_idea)
    return {
        "category": result["category"],
        "category_source": "ai",
        "category_confidence": result["confidence"],
        "government_contracting_signal": result["government_contracting_signal"],
    }

@app.get("/start", response_class=HTMLResponse)
async def start(request: Request):
    """Step 2: shows the deliverables recap + pricing + Checkout
    button for the idea the customer just entered on the Step 1 landing
    page. The idea travels here as a query param (Step 1's JS reads it back
    out of localStorage before navigating) rather than a Firestore lookup -
    no order exists yet at this point."""
    # Carries the RA choice made on page 1's hero form (see _hero_idea_form.html's
    # ?ra= param) so Step 2 starts pre-selected on whatever the customer already
    # picked, instead of always resetting to the default.
    # Default is "self" (unchecked professional RA) - the customer must actively
    # opt in to the paid RA add-on rather than having it pre-selected for them.
    preselected_ra_choice = "professional_ra" if request.query_params.get("ra") == "professional_ra" else "self"
    return templates.TemplateResponse(request, "start.html", {
        "business_idea": request.query_params.get("idea", ""),
        "preselected_ra_choice": preselected_ra_choice,
        **_wizard_context(),
    })

@app.post("/start", response_class=HTMLResponse)
async def start_checkout(request: Request):
    """Creates the draft order and a Stripe Checkout Session from just the
    business idea - replaces /launch and /launch-pr. Everything else
    (name, personal info, business details) is collected post-payment in
    the dashboard (Steps 3-5). Checkout collects email/phone itself and
    already renders Apple Pay/Google Pay natively above the card form, so
    there's no separate PaymentIntent/Payment Request Button path anymore."""
    form_raw = await request.form()
    form = dict(form_raw)

    errors = {}
    if form.get("consent") != "on":
        errors["consent"] = "Please agree to the terms to continue"
    if errors:
        return templates.TemplateResponse(request, "form_errors.html", {
            "errors": errors,
            "all_fields": CHECKOUT_VALIDATED_FIELDS,
        })

    # index.html's hero idea form (_hero_idea_form.html, shared with the
    # SEO landing pages) is the sole pre-payment field - it saves the idea
    # to localStorage and redirects here with ?idea=... (see /start GET),
    # which start.html carries into a hidden field on this form. Still not
    # server-side required though: the hero form's own client-side check
    # (10+ chars) is what makes it "the" pre-payment field in practice, and
    # staying lenient here means a customer who lands on /start some other
    # way (e.g. a bookmark, or a future entry point with no idea capture)
    # never gets stuck unable to pay.
    business_idea = form.get("business_idea", "").strip()
    lead_id = (form.get("lead_id") or "").strip()

    charge_amount = LLC_FORMATION_PRICE_CENTS

    # Registered agent choice - collected here, pre-payment, since the $110/yr
    # charge (if any) has to be part of this same Checkout Session (see Part 4).
    # Anything other than the one value the radio group's "self" option submits
    # is treated as professional_ra, same lenient pattern as the idea field
    # above - a tampered/missing value must never silently fail checkout.
    registered_agent_choice = "self" if form.get("registered_agent_choice") == "self" else "professional_ra"
    ra_fields = {"registered_agent_choice": registered_agent_choice}
    if registered_agent_choice == "professional_ra":
        ra_fields["ra_annual_fee_cents"] = RA_ANNUAL_FEE_CENTS

    order_ref = ORDERS.document()
    order_id = order_ref.id
    # run_id groups every AI Operations Evidence Layer event for this order
    # (see app/ai_ops.py) - generated here, at the very first moment the
    # order exists, and persisted on the order doc itself (below) so every
    # later step just reads it back via ai_ops.set_current_order instead of
    # threading it through every function signature.
    run_id = ai_ops.new_run_id()
    ai_ops.set_current_order(order_id, run_id)
    ai_ops.emit_event(
        "idea_intake", "started", step_label="Business Idea Captured",
        operation="prepare", autonomy="autonomous", actor="system",
        input_summary=f"business idea, {len((business_idea or '').strip())} chars",
    )
    # Written immediately, before the Stripe call below - so the idea is
    # never lost even if checkout-session creation fails or the browser
    # closes before the redirect. No business_name/first_name/email yet -
    # those come from Steps 3-4, post-payment; Stripe Checkout collects
    # email itself during payment.
    extra = {"lead_id": lead_id} if lead_id else {}
    attribution = parse_attribution_cookie(request.cookies.get(ATTRIBUTION_COOKIE_NAME))
    # The idea is normally already present here now (see the comment
    # above - index.html's hero form is the pre-payment field again), so
    # this is the common classification point, not the rare one; a blank
    # idea (bookmarked /start, or a future entry point with no idea
    # capture) just skips classification until Step 3's update-idea runs
    # it instead.
    category_fields = _classify_category_fields(business_idea)
    record_state(order_ref, "draft", business_idea=business_idea, run_id=run_id, **extra, **attribution, **category_fields,
                 **ra_fields,
                 created_at=firestore.SERVER_TIMESTAMP, checkout_at=firestore.SERVER_TIMESTAMP,
                 awaiting_intake=True,
                 consent=True, consent_at=firestore.SERVER_TIMESTAMP)
    ai_ops.emit_event(
        "idea_intake", "completed", step_label="Business Idea Captured",
        operation="prepare", autonomy="autonomous", actor="system", status="success",
        output_summary="order created, business idea captured",
    )
    # A genuine human decision, not a review of AI-suggested work (unlike
    # e.g. the business-category confirmation) - the customer alone picks
    # between self and the $110/yr professional RA, with no AI involved in
    # that choice at all, so this is its own step key rather than the
    # generic "human_review" used for approve/edit checkpoints elsewhere.
    ai_ops.emit_event(
        "ra_selection", "completed", step_label="Registered Agent Selection",
        operation="review", autonomy="human_only", actor="human", status="success",
        output_summary=f"customer chose {registered_agent_choice} as registered agent",
        human_touched=True, human_action=registered_agent_choice,
    )

    base_url = str(request.base_url)
    try:
        session = create_checkout_session(
            order_id=order_id,
            success_url=f"{base_url}success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}cancel",
            amount=charge_amount,
            gclid=attribution.get("gclid"),
            utm_source=attribution.get("utm_source"),
            utm_medium=attribution.get("utm_medium"),
            utm_campaign=attribution.get("utm_campaign"),
            registered_agent_choice=registered_agent_choice,
        )
        order_ref.set({"stripe_checkout_session_id": session.id}, merge=True)
    except Exception as e:
        # Previously uncaught here - any Stripe failure (bad key, outage,
        # rate limit) crashed with a raw 500 that htmx silently drops
        # without swapping anything in, so the customer just saw the
        # button spin and stop with no explanation at all.
        print(f"⚠️ Could not create Stripe checkout session for order {order_id}: {e}")
        return templates.TemplateResponse(request, "form_errors.html", {
            "errors": {"_checkout": "Something went wrong starting checkout - please try again in a moment, or contact support@launchbridge.ai if this keeps happening."},
            "all_fields": CHECKOUT_VALIDATED_FIELDS,
        })

    return Response(status_code=200, headers={"HX-Redirect": session.url})

def needs_ssn(order: dict) -> bool:
    """True if this order still needs an EIN application filed with the
    IRS - the only step that actually requires an SSN. skip_ein customers
    already have their own EIN and are never asked for one."""
    return not order.get("skip_ein")

def needs_ssn_reentry(order: dict, order_id: str) -> bool:
    """True if this order needs an EIN, has reached the point where EIN
    filing is actually eligible (filing_confirmed), hasn't gotten one yet,
    and there's currently no encrypted SSN on file for it.

    Covers both the 72-hour vault expiry (ssn_expired gets set explicitly
    by ssn_expiry_scheduler) and older orders that lost their SSN before
    the vault existed at all - those have a stale ein_error but ssn_expired
    was never set, since that flag didn't exist yet when they got stuck.
    Recomputing from current state instead of trusting only the flag means
    any such order is rescued automatically rather than needing a manual
    Firestore patch."""
    state = order.get("state", "draft")
    return (
        needs_ssn(order)
        and reached(state, "filing_confirmed")
        and not reached(state, "ein_requested")
        and not is_ssn_stored(order_id)
    )

def process_paid_order(order_id: str, payment_status: str, background_tasks: BackgroundTasks, subscription_id: str = None) -> bool:
    """Advances an order past payment and kicks off the real pipeline -
    shared by /success (the customer's browser redirect) and /webhook
    (Stripe's own server-to-server notification), since either one might
    be what actually notices the payment first.

    Idempotent by construction: only acts if the order is still "draft".
    Whichever of /success or /webhook reaches this first does the real
    work; the other is a no-op. Returns True if it actually advanced the
    order, False if there was nothing to do (already processed, order
    missing, or payment not actually confirmed paid).

    If the order will eventually need an EIN, the SSN hasn't been
    collected yet at all - intake no longer asks for it (see
    /collect-ssn) - so the agent pipeline itself is deferred until the
    customer submits it; this just marks the order "paid" and sets
    awaiting_ssn so /success and /status know to send them there first.
    Both /success and /webhook can reach here for the same order (the
    usual race), so the "we received your order" email is sent exactly
    once, right here, regardless of which path wins."""
    order_ref = ORDERS.document(order_id)
    order_snap = order_ref.get()
    if not order_snap.exists:
        return False
    order = order_snap.to_dict()

    if order.get("state") != "draft" or payment_status != "paid":
        return False

    order_ref.set({"paid_at": firestore.SERVER_TIMESTAMP}, merge=True)
    # Set once the mixed-cart Checkout Session actually creates the RA
    # subscription (mode="subscription", professional_ra orders only) - see
    # Part 4/5 of the RA feature. Not present at all for self-RA orders.
    if subscription_id:
        order_ref.set({"ra_subscription_id": subscription_id, "ra_subscription_status": "active"}, merge=True)

    # Stripe Checkout collects the customer's email itself (see /start) -
    # nothing else in this pipeline ever wrote it back onto the order, so
    # without this every order's email field stayed empty forever: no
    # confirmation/magic-link email, no auto-login on /success, and no way
    # to look the order up from the "sign in" form either. Re-fetched by
    # session ID here (not passed in) so both /success and /webhook - either
    # of which can be first to reach this function - land on the same result.
    checkout_session = None
    if not order.get("email") and order.get("stripe_checkout_session_id"):
        try:
            checkout_session = retrieve_checkout_session(order["stripe_checkout_session_id"])
            customer_email = checkout_session.customer_details.email if checkout_session.customer_details else None
        except Exception as e:
            print(f"⚠️ Could not retrieve Stripe session to capture email for order {order_id}: {e}")
            customer_email = None
        if customer_email:
            order_ref.set({"email": customer_email, "customer_email": customer_email}, merge=True)
            order["email"] = customer_email
            order["customer_email"] = customer_email

    send_order_received_email(order, order_id)
    # business_name/full_name don't exist yet at this point (Steps 3-4,
    # post-payment) - business_idea is the only descriptive field known
    # this early, captured back at Step 1.
    #
    # amount_paid uses the real Stripe amount_total (same fix pattern as the
    # GA4 purchase event - see success_interstitial.html/dashboard_name.html) -
    # LLC_FORMATION_PRICE_CENTS alone under-reports by the $110/yr RA fee for
    # every professional_ra order. Reuses the session already fetched above
    # when email capture needed it; otherwise fetches it fresh here. Falls
    # back to the base price only if the Stripe call itself fails, so a
    # transient API error can never crash or blank out this notification.
    amount_paid = LLC_FORMATION_PRICE_CENTS // 100
    if checkout_session is None and order.get("stripe_checkout_session_id"):
        try:
            checkout_session = retrieve_checkout_session(order["stripe_checkout_session_id"])
        except Exception as e:
            print(f"⚠️ Could not retrieve Stripe session for order {order_id} (SMS amount falling back to base price): {e}")
    if checkout_session and checkout_session.amount_total:
        amount_paid = checkout_session.amount_total // 100

    if APP_ENV == "staging":
        send_admin_sms(
            f"🧪 [TEST] New payment! {order.get('business_idea', '')[:50]} - ${amount_paid} paid. "
            "Staging order - not real money."
        )
    else:
        send_admin_sms(
            f"💰 [LIVE] New payment! {order.get('business_idea', '')[:50]} - ${amount_paid} paid. "
            f"Order: {order_id[:8]}. Admin: app.launchbridge.ai/admin"
        )

    # awaiting_intake orders collected only 5 fields pre-payment. The rest
    # (address, DOB, sig) is gathered in the dashboard before the pipeline
    # can advance. Name check only needs business_name so it runs right
    # away regardless; document generation waits (see run_document_generation).
    awaiting_intake = bool(order.get("awaiting_intake"))

    if needs_ssn(order):
        record_state(order_ref, "paid", awaiting_ssn=True)
        if not awaiting_intake:
            # Legacy pre-wizard orders only (see run_name_check's docstring) -
            # every order created via /start has no business_name at all
            # yet at this point; the customer picks and clears one in the
            # Step 3 dashboard page, post-payment.
            background_tasks.add_task(run_name_check, order_id)
        return True

    if order.get("skip_llc_formation"):
        if not awaiting_intake:
            background_tasks.add_task(run_early_assets, order_id)
        trigger_assets = advance_past_filing_confirmed(order_ref, order)
        if trigger_assets:
            background_tasks.add_task(run_asset_generation, order_id)
    else:
        record_state(order_ref, "paid")
        if not awaiting_intake:
            background_tasks.add_task(run_name_check, order_id)

    return True

def start_pipeline_after_ssn(order_id: str, background_tasks: BackgroundTasks):
    """Picks up exactly where process_paid_order left off for an order
    that was waiting on an SSN - clears awaiting_ssn and kicks off the
    same pipeline process_paid_order would have started immediately had
    the SSN not been needed. ssn_collected_at is set by encrypt_ssn
    itself, not here.

    For new wizard orders (awaiting_intake=True), Steps 3-5 (name, personal
    info, business details) haven't all been collected yet - the pipeline
    stays paused until Step 5 (POST /dashboard/orders/{id}/business) clears
    that flag."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return

    order_ref.set({"awaiting_ssn": False}, merge=True)

    if order.get("awaiting_intake"):
        return

    if order.get("skip_llc_formation"):
        if not order.get("early_assets_done"):
            background_tasks.add_task(run_early_assets, order_id)
        trigger_assets = advance_past_filing_confirmed(order_ref, order)
        if trigger_assets:
            background_tasks.add_task(run_asset_generation, order_id)
    else:
        background_tasks.add_task(run_name_check, order_id)

def resume_ein_after_ssn_reentry(order_id: str):
    """Picks up after a customer re-enters an SSN that had expired
    (ssn_expired) - unlike start_pipeline_after_ssn, the order has
    already progressed past name/document/filing steps, so re-running
    those would be wrong. The only thing actually waiting on the SSN is
    the EIN filing itself - and that still waits for an explicit "Apply
    for EIN" click (dashboard, or the one-click email link), same as the
    normal flow. This just clears ssn_expired, advances the generic
    filing_confirmed to the more specific awaiting_ein_filing, and
    re-notifies the admin that filing can now proceed."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return

    order_ref.set({"ssn_expired": False}, merge=True)

    if order.get("state") == "filing_confirmed":
        record_state(order_ref, "awaiting_ein_filing")
        order["state"] = "awaiting_ein_filing"

    if order.get("state") in EIN_ELIGIBLE_STATES:
        notify_ein_ready(order, order_id)

@app.get("/success")
async def success(request: Request, background_tasks: BackgroundTasks,
                  session_id: str = None, order_id: str = None):
    resolved_order_id = order_id

    if session_id:
        try:
            session = retrieve_checkout_session(session_id)
        except Exception as e:
            # A customer just paid and we can't confirm it - worth a page,
            # not just a quiet log line.
            log_customer_error(request, e)
            return friendly_error_response(request, CUSTOMER_FRIENDLY_ERROR, 400)

        resolved_order_id = session.client_reference_id
        if not resolved_order_id:
            return RedirectResponse(url="/")

        process_paid_order(resolved_order_id, session.payment_status, background_tasks,
                            subscription_id=session.subscription)

    if not resolved_order_id:
        return RedirectResponse(url="/")

    order_ref = ORDERS.document(resolved_order_id)
    order = order_ref.get().to_dict()
    if not order:
        return RedirectResponse(url="/")

    # GA4 purchase event data - the real Stripe amount_total (not a
    # hardcoded formation-price constant, which would under-report by the
    # $110/yr RA fee for professional_ra orders), computed once and marked
    # sent so it can never double-fire regardless of which of the two
    # rendering paths below actually shows it. Only available when this
    # request came from Stripe's own redirect (session_id set) - the
    # order_id-only entry has no Stripe session to pull a real amount from.
    purchase_event = None
    if session_id and not order.get("purchase_event_sent"):
        purchase_event = {
            "value": round((session.amount_total or 0) / 100, 2),
            "ra_selected": order.get("registered_agent_choice", "self"),
        }

    # Auto-login straight into Step 3 naming - this is the one place a
    # completed Stripe Checkout is allowed to mint a dashboard session
    # directly, since it's the same browser tab that just finished paying
    # (never done from /webhook, which is server-to-server with no browser
    # to hand a session to). Accepted tradeoff: someone who mistypes a
    # different email into Stripe Checkout would have their own browser see
    # that one order - limited blast radius, not broader account access.
    # The magic-link email (send_order_received_email, inside
    # process_paid_order above) still goes out as a durable fallback in
    # case this redirect fails or the tab was already closed.
    email = order.get("email", "")
    if email:
        try:
            magic_url = create_magic_link(email)
            verify_path = "/dashboard/verify" + magic_url.split("/dashboard/verify", 1)[1]
            if purchase_event:
                # This redirect (then /dashboard/verify's own redirect) means
                # the purchase event can't fire on this response directly -
                # stashed on the order doc so Step 3 (dashboard_name, always
                # the first page a freshly-paid order reaches) can fire it
                # once and clear it. See dashboard_name below.
                order_ref.set({"pending_purchase_event": purchase_event, "purchase_event_sent": True}, merge=True)
            return RedirectResponse(url=verify_path, status_code=303)
        except Exception as e:
            print(f"⚠️ Could not auto-login order {resolved_order_id} after payment: {e}")

    if purchase_event:
        order_ref.set({"purchase_event_sent": True}, merge=True)

    return templates.TemplateResponse(request, "success_interstitial.html", {
        "email": email,
        "order_id": resolved_order_id,
        "business_name": order.get("business_name", ""),
        "purchase_event": purchase_event,
    })

def import_ad_conversion(order_id: str, gclid: str | None, amount_cents: int, conversion_dt: datetime.datetime):
    """Reports a completed Stripe payment to Google Ads against its gclid -
    triggered from the webhook (not /success), since the webhook is the
    server-authoritative record of payment rather than something that
    depends on the customer's browser/ad-blocker actually firing a
    client-side gtag('event', 'purchase') call. A silent no-op for the
    (normal, majority) case where this order never carried a gclid at all -
    not every order is ad-driven.

    Idempotent: ad_conversion_imported is only ever attempted once per
    order, since Stripe can redeliver the same webhook event on a retry and
    a second import would double-count the same purchase in Google Ads.
    Wrapped in try/except on top of upload_click_conversion's own internal
    guard - this must never be able to affect the webhook's core job of
    marking the order paid, which has already happened by the time this
    runs (see stripe_webhook, called via background_tasks)."""
    if not gclid:
        return
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order or order.get("ad_conversion_imported"):
        return
    try:
        success = upload_click_conversion(
            gclid=gclid, order_id=order_id,
            conversion_value=amount_cents / 100, conversion_datetime=conversion_dt,
        )
        order_ref.set({
            "ad_conversion_imported": success,
            "ad_conversion_attempted_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)
    except Exception as e:
        print(f"⚠️ Google Ads conversion import crashed for order {order_id}: {e}")
        order_ref.set({"ad_conversion_attempted_at": firestore.SERVER_TIMESTAMP}, merge=True)

def _find_order_by_ra_subscription(subscription_id: str):
    """Looks up the order carrying this RA subscription ID (set by
    process_paid_order once the mixed-cart Checkout Session confirms it) -
    invoice.payment_failed/customer.subscription.deleted events don't carry
    our order_id directly, only the Stripe subscription ID, so this is how
    the webhook maps one back to the other. Returns (None, None) if no
    matching order exists (e.g. a stale/unrelated subscription event)."""
    matches = list(ORDERS.where("ra_subscription_id", "==", subscription_id).limit(1).stream())
    if not matches:
        return None, None
    return matches[0].reference, matches[0].to_dict()

@app.post("/webhook")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks):
    """Server-to-server backstop for /success - if a customer closes their
    tab right after paying, before Checkout's success_url redirect fires,
    /success never runs and the order would otherwise sit at "draft"
    forever despite Stripe having actually been paid. This is the
    reliable path; /success is just the fast path when it works.

    Every received event is logged to Firestore (webhook_events) for
    evidence, whether or not it was a checkout.session.completed event and
    whether or not signature verification passed."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = construct_webhook_event(payload, sig_header)
    except Exception as e:
        db.collection("webhook_events").add({
            "verified": False, "error": str(e), "received_at": firestore.SERVER_TIMESTAMP,
            "app_env": APP_ENV,
        })
        return Response(status_code=400)

    log_entry = {
        "verified": True, "event_id": event.id, "event_type": event.type,
        "received_at": firestore.SERVER_TIMESTAMP, "app_env": APP_ENV,
    }

    if event.type == "checkout.session.completed":
        session = event.data.object
        order_id = session.client_reference_id
        log_entry["order_id"] = order_id
        if order_id:
            advanced = process_paid_order(order_id, session.payment_status, background_tasks,
                                           subscription_id=session.subscription)
            log_entry["advanced_order"] = advanced
            if session.payment_status == "paid":
                # StripeObject (session.metadata's type) doesn't actually
                # implement .get() despite looking dict-like - it falls
                # through to __getattr__/__getitem__ and raises AttributeError
                # instead of returning None. to_dict() is a real dict.
                gclid = session.metadata.to_dict().get("gclid") if session.metadata else None
                conversion_dt = datetime.datetime.fromtimestamp(event.created, tz=datetime.timezone.utc)
                background_tasks.add_task(
                    import_ad_conversion, order_id, gclid, session.amount_total, conversion_dt,
                )

    elif event.type == "invoice.payment_failed":
        invoice = event.data.object
        subscription_id = invoice.get("subscription")
        log_entry["subscription_id"] = subscription_id
        if subscription_id:
            order_ref, order = _find_order_by_ra_subscription(subscription_id)
            if order_ref:
                log_entry["order_id"] = order_ref.id
                order_ref.set({"ra_subscription_status": "past_due"}, merge=True)
                business_label = order.get("business_name") or order.get("business_idea", "")[:50] or order_ref.id[:8]
                send_admin_sms(
                    f"⚠️ RA renewal payment FAILED for {business_label} (order {order_ref.id[:8]}). "
                    "Registered agent coverage may lapse - follow up with the customer."
                )

    elif event.type == "customer.subscription.deleted":
        subscription = event.data.object
        subscription_id = subscription.id
        log_entry["subscription_id"] = subscription_id
        order_ref, order = _find_order_by_ra_subscription(subscription_id)
        if order_ref:
            log_entry["order_id"] = order_ref.id
            order_ref.set({"ra_subscription_status": "canceled"}, merge=True)
            business_label = order.get("business_name") or order.get("business_idea", "")[:50] or order_ref.id[:8]
            send_admin_sms(
                f"🚫 RA subscription CANCELED for {business_label} (order {order_ref.id[:8]}). "
                "This LLC now has no registered agent on file - follow up immediately."
            )

    db.collection("webhook_events").add(log_entry)
    return Response(status_code=200)

def ensure_payment_link(order_ref, order: dict) -> dict:
    """run_asset_generation deliberately skips creating the payment link
    until the connected Stripe account is actually active (see the
    comment there) - this is what actually creates it once that happens,
    triggered by the customer (or admin) simply viewing the status page
    again after finishing Stripe's onboarding flow. No separate poller
    needed since the status page is exactly where they'd check next."""
    connect_id = order.get("stripe_connect_account_id")
    if not connect_id or order.get("stripe_payment_link_url"):
        return order
    if not is_account_active(connect_id):
        return order
    try:
        payment_link_url = create_pay_what_you_want_payment_link(connect_id, order.get("business_name", ""))
        order_ref.set({"stripe_payment_link_url": payment_link_url}, merge=True)
        order["stripe_payment_link_url"] = payment_link_url
    except Exception as e:
        print(f"⚠️ Could not create payment link for order {order_ref.id} after onboarding: {e}")
    return order

def status_context(order_id: str, order: dict) -> dict:
    """Shared between the full status page and the auto-refreshed timeline
    partial, so the two never drift out of sync with each other."""
    state = order.get("state", "draft")
    return {
        "order_id": order_id,
        "state": state,
        "state_message": compute_state_message(order, state),
        "timeline": compute_timeline(order, state),
        "estimated_completion": estimate_completion(order, state),
        "estimate_breakdown": "LLC approval: 1-3 business days · EIN: same day after approval · Website: built and published by our team",
        "last_updated": datetime.datetime.now().strftime("%I:%M:%S %p").lstrip("0"),
    }

def _rate_limited(collection_name: str, key_field: str, key_value: str, window: datetime.timedelta, limit: int) -> bool:
    """Shared by every per-key rate limit in this file (lost-order-ID
    requests, status-page IP throttling, failed status-email attempts).
    True means the caller is over the limit and should be rejected -
    in that case nothing is logged, so a flood past the limit doesn't
    keep inflating the log. Otherwise this attempt itself is logged (it
    counts toward the next check) and False is returned.

    Filters by timestamp in Python rather than adding a second Firestore
    inequality clause on top of the equality filter - that would need a
    composite index, and volume per key is small enough that
    fetch-then-filter is simpler to ship and just as correct."""
    log = db.collection(collection_name)
    window_start = datetime.datetime.now(datetime.timezone.utc) - window
    existing = log.where(key_field, "==", key_value).stream()
    recent = [r for r in existing if (r.to_dict().get("at") or window_start) > window_start]
    if len(recent) >= limit:
        return True
    log.add({key_field: key_value, "at": firestore.SERVER_TIMESTAMP})
    return False

def get_client_ip(request: Request) -> str:
    """Cloud Run sits behind Google's own proxy, so request.client.host is
    that proxy, not the real visitor - the actual client IP is the first
    hop in X-Forwarded-For instead."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

EIN_FILING_LINK_TTL_SECONDS = 86400  # 24 hours - regenerated fresh each time notify_ein_ready fires

def _make_ein_filing_link_token(order_id: str) -> str:
    """Same stateless signed-claim scheme as dashboard_auth's magic links,
    with an "ein-filing:" prefix baked into the signed payload - sharing
    STATUS_SESSION_SECRET across token types is fine precisely because
    each payload says what kind of claim it is, so one can never be
    replayed as the other even though both verify against the same
    secret."""
    expiry = int(datetime.datetime.now(datetime.timezone.utc).timestamp()) + EIN_FILING_LINK_TTL_SECONDS
    payload = f"ein-filing:{order_id}:{expiry}"
    sig = hmac.new(STATUS_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}:{sig}"

def _verify_ein_filing_link_token(order_id: str, token: str) -> bool:
    if not token or ":" not in token:
        return False
    expiry_str, _, sig = token.partition(":")
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if expiry < datetime.datetime.now(datetime.timezone.utc).timestamp():
        return False
    expected = hmac.new(STATUS_SESSION_SECRET.encode(), f"ein-filing:{order_id}:{expiry}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)

def notify_ein_ready(order: dict, order_id: str):
    """Fired the moment an order has both a filed-and-approved LLC and a
    stored SSN - the only two prerequisites for EIN filing. Tells the
    admin via SMS and a one-click email; the actual filing still waits for
    an explicit "Apply for EIN" click (in the dashboard, or via the
    email's link) - this never files anything by itself."""
    business_name = order.get("business_name", "")
    irs_note = "IRS open til 10pm ET" if is_irs_open() else "IRS closed - will auto-file when open"
    send_admin_sms(f"🔐 SSN ready! {business_name} - open Chrome and file EIN now. {irs_note}. Admin: app.launchbridge.ai/admin")
    ein_filing_url = f"https://app.launchbridge.ai/admin/ein-filing-link/{order_id}?token={_make_ein_filing_link_token(order_id)}"
    send_ein_filing_ready_email(order, order_id, ein_filing_url)

# ─── Customer dashboard (magic-link auth) ──────────────────────────────
# Replaced /status/*, /collect-ssn/*, /request-order-id, and the four
# /download-* routes (see the security-rework plan) - ownership is now
# checked per-request via get_owned_order instead of a long-lived
# email-verification cookie scoped to one order_id.

DASHBOARD_SESSION_COOKIE = "lb_session"
MAGIC_LINK_RATE_WINDOW = datetime.timedelta(hours=1)
MAGIC_LINK_RATE_LIMIT = 3

# Stopgap app-level throttle on the two Gemini/SCC-calling endpoints in this
# file - there's no Cloud Armor/WAF in front of Cloud Run yet, so these are
# the only thing standing between an abused dashboard session and unbounded
# Gemini API cost / SCC scraping. Keyed per order_id (the natural unit here,
# since every call site is already order-scoped via get_owned_order) rather
# than per IP, so it can't be defeated by rotating IPs behind the same
# session. 10/hour comfortably covers real usage - a customer refining their
# idea a few times or retrying a couple of rejected names - while still
# capping the cost of a compromised/scripted session. Replace with Cloud
# Armor rate-limiting at the infra layer when that's set up; this isn't a
# substitute for it.
NAME_IDEAS_RATE_WINDOW = datetime.timedelta(hours=1)
NAME_IDEAS_RATE_LIMIT = 10
SCC_NAME_CHECK_RATE_WINDOW = datetime.timedelta(hours=1)
SCC_NAME_CHECK_RATE_LIMIT = 10

def get_owned_order(order_id: str, request: Request):
    """Every failure mode - no session, expired session, order doesn't
    exist, order belongs to a different email - returns the identical
    404. A cross-customer guess at another order_id must be
    indistinguishable from a typo, so this never returns 403."""
    customer_id = verify_and_touch_session(request.cookies.get(DASHBOARD_SESSION_COOKIE, ""))
    if not customer_id:
        raise HTTPException(status_code=404)
    order_ref = ORDERS.document(order_id)
    order_snap = order_ref.get()
    if not order_snap.exists:
        raise HTTPException(status_code=404)
    order = order_snap.to_dict()
    if (order.get("email") or "").strip().lower() != customer_id:
        raise HTTPException(status_code=404)
    return order_ref, order, customer_id

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_login(request: Request):
    return templates.TemplateResponse(request, "dashboard_login.html", {})

@app.post("/dashboard/login", response_class=HTMLResponse)
async def dashboard_login_submit(request: Request):
    """Always returns the identical "check your email" response and
    always queries Firestore for a matching order, whether or not the
    email is real and whether or not the rate limit was already hit -
    so neither response shape nor timing reveals which emails have
    orders, or that a sender has been rate-limited."""
    form = await request.form()
    email = (form.get("email") or "").strip().lower()

    if email:
        over_limit = _rate_limited("magic_link_requests", "email", email, MAGIC_LINK_RATE_WINDOW, MAGIC_LINK_RATE_LIMIT)
        has_order = next(iter(ORDERS.where("email", "==", email).limit(1).stream()), None) is not None
        if has_order and not over_limit:
            send_magic_link_email(email, create_magic_link(email))

    return templates.TemplateResponse(request, "dashboard_check_email.html", {})

@app.get("/dashboard/verify", response_class=HTMLResponse)
async def dashboard_verify(request: Request, token: str = "", exp: str = "", sig: str = "", next: str = ""):
    email = redeem_magic_link(token, exp, sig)
    if not email:
        return templates.TemplateResponse(request, "dashboard_login.html", {
            "error": "This link is invalid or has expired - request a new one below.",
        }, status_code=400)

    session_id = create_session(email)
    # `next` only ever comes from a link we generated ourselves (recovery
    # emails - see send_mid_flow_recovery_email), but validate it anyway
    # since it's still attacker-controlled input on the URL: must be a
    # same-site dashboard path, never an absolute/external URL.
    safe_next = next if next.startswith("/dashboard/orders/") and not next.startswith("//") else ""
    response = RedirectResponse(url=safe_next or "/dashboard/orders", status_code=303)
    response.set_cookie(
        key=DASHBOARD_SESSION_COOKIE, value=session_id,
        max_age=SESSION_ABSOLUTE_SECONDS, httponly=True, secure=True, samesite="lax",
        path="/",
    )
    return response

@app.get("/dashboard/logout")
async def dashboard_logout(request: Request):
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    if session_id:
        delete_session(session_id)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.delete_cookie(DASHBOARD_SESSION_COOKIE, path="/")
    return response

@app.get("/dashboard/orders", response_class=HTMLResponse)
async def dashboard_orders(request: Request):
    customer_id = verify_and_touch_session(request.cookies.get(DASHBOARD_SESSION_COOKIE, ""))
    if not customer_id:
        return RedirectResponse(url="/dashboard")

    orders = [doc for doc in ORDERS.where("email", "==", customer_id).stream()]
    if len(orders) == 1:
        order_id = orders[0].id
        # Land mid-flow customers back on the step they haven't finished yet
        # (fresh off payment or reopening their magic link later) instead of
        # the fulfillment status page, which has nothing to show them yet.
        next_step = next_incomplete_step_url(order_id, orders[0].to_dict())
        return RedirectResponse(url=next_step or f"/dashboard/orders/{order_id}")

    return templates.TemplateResponse(request, "dashboard_order_list.html", {
        "orders": [{"order_id": doc.id, "business_name": doc.to_dict().get("business_name")} for doc in orders],
    })

def _dashboard_order_context(
    request: Request, order_ref, order: dict,
    ssn_error: str = None,
    intake_errors: dict = None,
    intake_form_data: dict = None,
) -> dict:
    """Shared between the order page, SSN-submit, and complete-intake routes."""
    order_id = order_ref.id
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    ssn_expired = bool(order.get("ssn_expired")) or needs_ssn_reentry(order, order_id)

    # Articles of Organization and Operating Agreement are emailed, not
    # downloadable from the dashboard (see the email-notice block in
    # dashboard_order.html) - every other generated document (certificate,
    # EIN letter, brand kit, logo) keeps its download button. Computed once
    # here (one GCS existence check per doc type) and reused below, rather
    # than DOCUMENT_LABELS itself excluding those two - the download route
    # and email pipeline still need the full label set.
    available_documents = {
        doc_id: _document_object_name(order, order_id, doc_id) is not None
        for doc_id in DOCUMENT_LABELS
    }
    downloadable_labels = {k: v for k, v in DOCUMENT_LABELS.items() if k not in ("articles", "operating_agreement")}

    # The articles/operating_agreement PDFs are AI-drafted right after
    # intake (see run_asset_generation) - available_documents.get('articles')
    # goes true then, long before Virginia SCC has actually approved the
    # filing. The "sent to your email" claim on the dashboard shouldn't
    # appear until it's actually true: filing_confirmed is when the SCC has
    # approved the LLC and the real, final documents would realistically
    # have been generated/emailed. Before that, the existing timeline
    # section below already says "Waiting for Virginia SCC to process your
    # filing" - no need for a second, redundant status line here.
    llc_documents_emailed = reached(order.get("state", "draft"), "filing_confirmed") and bool(
        available_documents.get("articles") or available_documents.get("operating_agreement"))

    needs_ssn_entry = (bool(order.get("awaiting_ssn")) or ssn_expired) and not order.get("awaiting_intake")
    # Threaded onto request.state so SecurityHeadersMiddleware (which only
    # sees the Request/Response, not this function's return value) can
    # serve a stricter script-src/connect-src CSP on this response - see
    # app/dashboard_security.py.
    request.state.needs_ssn_entry = needs_ssn_entry

    return {
        **status_context(order_id, order),
        "order": order,
        "business_name": order.get("business_name"),
        "full_name": order.get("full_name"),
        "email": order.get("email"),
        "registered_agent_choice": order.get("registered_agent_choice", "self"),
        "ein": order.get("ein"),
        "website_url": order.get("website_url"),
        "website_embeddable": check_iframe_embeddable(order["website_url"]) if order.get("website_url") else False,
        "onboarding_url": f"/connect/onboard/{order_id}" if order.get("stripe_connect_account_id") else None,
        "awaiting_intake": bool(order.get("awaiting_intake")),
        "google_places_api_key": GOOGLE_PLACES_API_KEY,
        "needs_ssn_entry": needs_ssn_entry,
        "ssn_expired": ssn_expired,
        "ssn_error": ssn_error,
        "intake_errors": intake_errors or {},
        "intake_form_data": intake_form_data or {},
        "csrf_token": make_csrf_token(session_id),
        "document_labels": downloadable_labels,
        "available_documents": available_documents,
        "llc_documents_emailed": llc_documents_emailed,
        # Separate from available_documents.values()|select|list, which
        # would also be true if only articles/operating_agreement (excluded
        # from downloadable_labels above) are ready - that must not render
        # an empty "Your Documents" card with no actual download links in it.
        "has_downloadable_documents": any(available_documents.get(doc_id) for doc_id in downloadable_labels),
        "ga_event": request.query_params.get("ga_event", ""),
    }

@app.get("/dashboard/orders/{order_id}", response_class=HTMLResponse)
async def dashboard_order(request: Request, background_tasks: BackgroundTasks, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    # A magic link, bookmark, or the order-list resolution above can land
    # here directly while Steps 3-5 aren't finished yet - this page no
    # longer has an intake form of its own to show them, so bounce forward
    # to wherever they actually left off.
    next_step = next_incomplete_step_url(order_id, order)
    if next_step:
        return RedirectResponse(url=next_step, status_code=303)
    order = ensure_payment_link(order_ref, order)
    # Background, not synchronous: a real Gemini call + Firebase deploy is
    # too slow to block this page load, but firing it here (rather than
    # waiting for stripe_activation_scheduler's hourly sweep) means a
    # customer who just finished Stripe onboarding usually sees their
    # payment button go live within seconds of landing back on this page,
    # not up to an hour later. check_and_update_website is a fast no-op if
    # there's nothing to do (no Stripe account, not active yet, or already live).
    if order.get("stripe_connect_account_id") and not order.get("payment_button_live"):
        background_tasks.add_task(check_and_update_website, order_id)
    return templates.TemplateResponse(request, "dashboard_order.html", _dashboard_order_context(request, order_ref, order))

# ── Step 3: name selection ──────────────────────────────────────────────────

@app.get("/dashboard/orders/{order_id}/name", response_class=HTMLResponse)
async def dashboard_name(request: Request, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    state = order.get("state", "draft")
    # Once a name is cleared, re-entering this page would let the customer
    # pick a different one after assets/filing have already started against
    # the first - forward-only, matches every other step's nav guard.
    # name_rejected is the one deliberate exception: SCC rejected the name
    # at filing time, so the customer must be able to come back here.
    if reached(state, "name_selected") and state != "name_rejected":
        return RedirectResponse(url=f"/dashboard/orders/{order_id}/details", status_code=303)

    # Fires the GA4 purchase event stashed by /success (see its own comment) -
    # Step 3 is always the first page a freshly-paid order reaches, whether
    # via the auto-login redirect or the customer's own magic-link email.
    # Cleared immediately so a page refresh/back-button never re-fires it.
    pending_purchase_event = order.get("pending_purchase_event")
    if pending_purchase_event:
        order_ref.set({"pending_purchase_event": firestore.DELETE_FIELD}, merge=True)

    business_idea = order.get("business_idea", "")
    name_ideas_error = None
    if not business_idea.strip():
        # Idea capture moved here (post-payment) - the pre-payment page no
        # longer collects it, so every order lands on this page with a
        # blank idea now, not just an edge case. Calling Gemini with
        # nothing would just waste a call and return generic junk - wait
        # for the customer to actually type something and hit "Update &
        # Regenerate Names" instead.
        name_ideas = []
    elif _rate_limited("name_ideas_requests", "order_id", order_id, NAME_IDEAS_RATE_WINDOW, NAME_IDEAS_RATE_LIMIT):
        name_ideas = []
        # _name_ideas_chips.html's default empty-state ("Generating name
        # ideas... refresh if this doesn't appear") is written for the
        # normal in-flight case and would be actively misleading here -
        # refreshing this page just re-triggers the same rate limit, it
        # doesn't make suggestions appear. Same wording as the
        # update-idea rate-limit message below for consistency.
        name_ideas_error = "You've hit the name-suggestion limit for this hour - please wait a bit, or just type your own name below."
    else:
        try:
            ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))
            loop = asyncio.get_event_loop()
            name_ideas = await ai_ops.run_in_executor_with_context(loop, None, generate_name_ideas, business_idea)
        except Exception as e:
            print(f"⚠️ Could not generate name ideas for order {order_id}: {e}")
            name_ideas = []

    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    return templates.TemplateResponse(request, "dashboard_name.html", {
        "order_id": order_id,
        "business_idea": business_idea,
        "name_ideas": name_ideas,
        "error": name_ideas_error,
        "no_idea_yet": not business_idea.strip(),
        "csrf_token": make_csrf_token(session_id),
        "name_error": None,
        "name_error_field": None,
        "skip_llc_formation": False,
        "submitted_name": "",
        "scc_confirmed": False,
        "name_rejected": state == "name_rejected",
        "business_name": order.get("business_name", ""),
        "pending_purchase_event": pending_purchase_event,
    })

@app.post("/dashboard/orders/{order_id}/update-idea", response_class=HTMLResponse)
async def dashboard_update_idea(request: Request, owned: tuple = Depends(get_owned_order)):
    """Step 1 - lets the customer refine their business idea and get fresh
    AI name suggestions without leaving the page, rather than being stuck
    with whatever one-liner they typed on the Step 1 landing page. Only
    updates business_idea + regenerates name_ideas; the customer still
    picks and submits an actual name via the main form below to advance,
    same as a normal page load of this step."""
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")

    form = await request.form()
    if not verify_csrf_token(session_id, (form.get("csrf_token") or "").strip()):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    business_idea = (form.get("business_idea") or "").strip()
    idea_error = validate_business_idea(business_idea)
    if idea_error:
        return templates.TemplateResponse(request, "_name_ideas_chips.html", {
            "name_ideas": [], "error": idea_error,
        })

    # Now that index.html's hero form captures the idea pre-payment again,
    # this is mainly an edit path (the customer refining what they already
    # typed on Step 1) rather than first-time capture - but it works
    # exactly the same either way, since it's just an unconditional
    # overwrite: reclassifies category fresh from whatever text is here
    # now, immediately with source "ai", well before Step 5 shows the
    # confirm-or-change UI built off it.
    ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))
    order_ref.set({"business_idea": business_idea, **_classify_category_fields(business_idea)}, merge=True)

    if _rate_limited("name_ideas_requests", "order_id", order_id, NAME_IDEAS_RATE_WINDOW, NAME_IDEAS_RATE_LIMIT):
        return templates.TemplateResponse(request, "_name_ideas_chips.html", {
            "name_ideas": [],
            "error": "You've regenerated names several times in the last hour - please wait a bit, or just type your own name below.",
        })

    loop = asyncio.get_event_loop()
    try:
        name_ideas = await ai_ops.run_in_executor_with_context(loop, None, generate_name_ideas, business_idea)
    except Exception as e:
        print(f"⚠️ Could not regenerate name ideas for order {order_id}: {e}")
        name_ideas = []

    return templates.TemplateResponse(request, "_name_ideas_chips.html", {
        "name_ideas": name_ideas,
    })

@app.post("/dashboard/orders/{order_id}/name", response_class=HTMLResponse)
async def dashboard_name_submit(request: Request, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    state = order.get("state", "draft")

    if reached(state, "name_selected") and state != "name_rejected":
        return RedirectResponse(url=f"/dashboard/orders/{order_id}/details", status_code=303)

    ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))

    form = await request.form()
    if not verify_csrf_token(session_id, (form.get("csrf_token") or "").strip()):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    business_idea = order.get("business_idea", "")
    skip_llc = form.get("skip_llc_formation") == "on"
    scc_confirmed = form.get("scc_confirmed") == "on"

    def render_error(error_msg: str, submitted: str, field: str = None):
        return templates.TemplateResponse(request, "dashboard_name.html", {
            "order_id": order_id,
            "business_idea": business_idea,
            "name_ideas": [],
            "csrf_token": make_csrf_token(session_id),
            "name_error": error_msg,
            "name_error_field": field,
            "skip_llc_formation": skip_llc,
            "submitted_name": submitted,
            "scc_confirmed": scc_confirmed,
            "name_rejected": state == "name_rejected",
            "business_name": order.get("business_name", ""),
        }, status_code=400)

    # check_name_on_scc below is automated and (as of the
    # CheckEntityDistinguishableCheckForOnline rewrite - see
    # app/agents/scc_name_check.py) reliable again, but it's still
    # best-effort/non-blocking rather than gating submission on it: the
    # customer self-certifies via this checkbox regardless, and our own
    # Playwright-driven verify_name_before_filing re-verifies before
    # anything is actually filed - see admin approve flow and
    # run_scc_filing. Kept as defense in depth rather than trusting a
    # single automated check to gate a real SCC filing.
    if not scc_confirmed:
        return render_error(
            "Please confirm you checked Virginia SCC availability before continuing.",
            form.get("existing_llc_name" if skip_llc else "business_name", ""),
            field="scc_confirmed",
        )

    # The customer's own attestation that they checked Virginia SCC
    # availability themselves - a real human confirmation checkpoint,
    # distinct from (and defense-in-depth on top of) the automated
    # name_scc_check below.
    ai_ops.emit_event(
        "human_review", "completed", step_label="Customer SCC Self-Certification",
        operation="review", autonomy="human_only", actor="human", status="success",
        output_summary="customer confirmed SCC availability check", human_touched=True,
        human_action="approve",
    )

    if skip_llc:
        existing_llc_name = (form.get("existing_llc_name") or "").strip()
        if not existing_llc_name:
            return render_error("Please enter your existing LLC's name.", "")
        # Best-effort, non-blocking - populates order.name_check for the
        # admin dashboard's "unverified" banner; never gates submission,
        # since it's expected to fail (reCAPTCHA-gated) most of the time.
        # Same shared throttle as the non-skip_llc branch below - skipping
        # the call when over the limit just means name_check falls back to
        # this placeholder instead of a real SCC lookup; the customer's own
        # scc_confirmed checkbox above (and our Playwright re-verify before
        # actual filing) is what actually gates things, so this never
        # blocks submission.
        if _rate_limited("scc_name_check_requests", "order_id", order_id, SCC_NAME_CHECK_RATE_WINDOW, SCC_NAME_CHECK_RATE_LIMIT):
            result = {"exists": None, "message": "Automated check skipped (rate limited) - relying on self-certification."}
        else:
            loop = asyncio.get_event_loop()
            try:
                result = await ai_ops.run_in_executor_with_context(loop, None, check_llc_exists_on_scc, existing_llc_name)
            except Exception as e:
                result = {"exists": None, "message": str(e)}
        order_ref.set({
            "business_name": existing_llc_name,
            "existing_llc_name": existing_llc_name,
            "skip_llc_formation": True,
            "name_check": result,
            "scc_self_confirmed": True,
        }, merge=True)
    else:
        submitted_name = (form.get("business_name") or "").strip()
        business_name, sanitize_error = sanitize_business_name(submitted_name)
        if sanitize_error:
            return render_error(sanitize_error, submitted_name)
        if _rate_limited("scc_name_check_requests", "order_id", order_id, SCC_NAME_CHECK_RATE_WINDOW, SCC_NAME_CHECK_RATE_LIMIT):
            result = {"status": "UNKNOWN", "message": "Automated check skipped (rate limited) - relying on self-certification."}
        else:
            loop = asyncio.get_event_loop()
            try:
                result = await ai_ops.run_in_executor_with_context(loop, None, check_name_on_scc, business_name)
            except Exception as e:
                result = {"status": "UNAVAILABLE", "message": str(e)}
        order_ref.set({
            "business_name": business_name,
            "skip_llc_formation": False,
            "name_check": result,
            "scc_self_confirmed": True,
            "name_rejected_reason": firestore.DELETE_FIELD,
        }, merge=True)

    record_state(order_ref, "name_selected", name_selected_at=firestore.SERVER_TIMESTAMP)

    if not order.get("awaiting_intake"):
        # Resubmitting a name after SCC rejected the original one (Steps
        # 4-5 were already completed before that happened, so they don't
        # need to be redone) - advance straight back to intake_complete so
        # the admin-approval/filing pipeline (which only watches for
        # intake_complete+ - see local_filing_poller.py) picks this order
        # up again. NOTE: this does not regenerate the brand kit/website/
        # documents already produced under the old (rejected) name -
        # they'll still reference it until someone re-runs
        # run_early_assets with those fields cleared first.
        record_state(order_ref, "intake_complete")
        return RedirectResponse(url=f"/dashboard/orders/{order_id}", status_code=303)

    return RedirectResponse(url=f"/dashboard/orders/{order_id}/details", status_code=303)

# ── Step 4: personal info ───────────────────────────────────────────────────

_STEP4_DETAILS_SIMPLE_FIELDS = [
    "first_name", "middle_name", "last_name", "phone", "dob",
    "address", "city", "zipcode", "county",
]

@app.get("/dashboard/orders/{order_id}/details", response_class=HTMLResponse)
async def dashboard_details(request: Request, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    if not reached(order.get("state", "draft"), "name_selected"):
        return RedirectResponse(url=f"/dashboard/orders/{order_id}/name", status_code=303)

    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    return templates.TemplateResponse(request, "dashboard_details.html", {
        "order_id": order_id,
        "business_name": order.get("business_name", ""),
        "order": order,
        "google_places_api_key": GOOGLE_PLACES_API_KEY,
        "csrf_token": make_csrf_token(session_id),
        "details_errors": {},
    })

@app.post("/dashboard/orders/{order_id}/details", response_class=HTMLResponse)
async def dashboard_details_submit(request: Request, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")

    if not reached(order.get("state", "draft"), "name_selected"):
        return RedirectResponse(url=f"/dashboard/orders/{order_id}/name", status_code=303)

    form_raw = await request.form()
    form = dict(form_raw)
    if not verify_csrf_token(session_id, (form.get("csrf_token") or "").strip()):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    errors = validate_step4_details(form)
    if errors:
        return templates.TemplateResponse(request, "dashboard_details.html", {
            "order_id": order_id,
            "business_name": order.get("business_name", ""),
            "order": {**order, **form},
            "google_places_api_key": GOOGLE_PLACES_API_KEY,
            "csrf_token": make_csrf_token(session_id),
            "details_errors": errors,
        }, status_code=400)

    parsed = parse_step4_details(form)
    safe_fields = {k: form[k] for k in _STEP4_DETAILS_SIMPLE_FIELDS if form.get(k)}
    order_ref.set({**safe_fields, **parsed}, merge=True)

    return RedirectResponse(url=f"/dashboard/orders/{order_id}/business", status_code=303)

@app.post("/dashboard/orders/{order_id}/ssn", response_class=HTMLResponse)
async def dashboard_submit_ssn(request: Request, background_tasks: BackgroundTasks, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")

    form = await request.form()
    if not verify_csrf_token(session_id, (form.get("csrf_token") or "").strip()):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    expired = bool(order.get("ssn_expired")) or needs_ssn_reentry(order, order_id)
    if not order.get("awaiting_ssn") and not expired:
        return RedirectResponse(url=f"/dashboard/orders/{order_id}", status_code=303)

    ssn = (form.get("ssn") or "").strip()
    error = validate_ssn(ssn)
    if not error and not encrypt_ssn(ssn, order_id):
        error = "Could not securely store your SSN - please try again, or contact support@launchbridge.ai if this keeps happening."

    if error:
        order = ensure_payment_link(order_ref, order)
        return templates.TemplateResponse(request, "dashboard_order.html",
            _dashboard_order_context(request, order_ref, order, ssn_error=error), status_code=400)

    dob = (form.get("dob") or "").strip()
    if dob and not order.get("dob"):
        order_ref.set({"dob": dob}, merge=True)

    if expired:
        resume_ein_after_ssn_reentry(order_id)
    else:
        start_pipeline_after_ssn(order_id, background_tasks)

    return RedirectResponse(url=f"/dashboard/orders/{order_id}", status_code=303)

# ── Step 5: business details ────────────────────────────────────────────────

_STEP5_BUSINESS_SIMPLE_FIELDS = [
    "business_purpose", "target_customer",
    "skip_ein", "existing_ein",
    "sig_first", "sig_middle", "sig_last",
    "industry_code", "duration",
]

@app.get("/dashboard/orders/{order_id}/business", response_class=HTMLResponse)
async def dashboard_business(request: Request, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    if not reached(order.get("state", "draft"), "name_selected"):
        return RedirectResponse(url=f"/dashboard/orders/{order_id}/name", status_code=303)
    if not order.get("awaiting_intake"):
        return RedirectResponse(url=f"/dashboard/orders/{order_id}", status_code=303)

    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    return templates.TemplateResponse(request, "dashboard_business.html", {
        "order_id": order_id,
        "business_name": order.get("business_name", ""),
        "order": order,
        "csrf_token": make_csrf_token(session_id),
        "business_errors": {},
    })

@app.post("/dashboard/orders/{order_id}/business", response_class=HTMLResponse)
async def dashboard_business_submit(
    request: Request, background_tasks: BackgroundTasks,
    owned: tuple = Depends(get_owned_order),
):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")

    if not order.get("awaiting_intake"):
        return RedirectResponse(url=f"/dashboard/orders/{order_id}", status_code=303)

    form_raw = await request.form()
    form = dict(form_raw)

    if not verify_csrf_token(session_id, (form.get("csrf_token") or "").strip()):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    errors = validate_post_payment_intake(form)
    if errors:
        return templates.TemplateResponse(request, "dashboard_business.html", {
            "order_id": order_id,
            "business_name": order.get("business_name", ""),
            "order": {**order, **form},
            "csrf_token": make_csrf_token(session_id),
            "business_errors": errors,
        }, status_code=400)

    parsed = parse_post_payment_intake(form)
    safe_fields = {k: form[k] for k in _STEP5_BUSINESS_SIMPLE_FIELDS if form.get(k)}

    # Also collect extra member signatures
    i = 2
    while True:
        first = form.get(f"extra_sig_first_{i}")
        if not first:
            break
        for suffix in ("first", "middle", "last"):
            v = form.get(f"extra_sig_{suffix}_{i}", "")
            if v:
                safe_fields[f"extra_sig_{suffix}_{i}"] = v
        i += 1

    # skip_ein is first collected here, but awaiting_ssn was already set at
    # payment time (needs_ssn(order) had no skip_ein to check yet) - clear
    # it now so a customer who already has an EIN is never asked for an SSN.
    skip_ein_now = bool(safe_fields.get("skip_ein"))

    order_ref.set({**safe_fields, **parsed,
                   "awaiting_intake": False,
                   "intake_at": firestore.SERVER_TIMESTAMP,
                   "intake_complete_at": firestore.SERVER_TIMESTAMP,
                   "consent_signature": True,
                   "consent_signature_at": firestore.SERVER_TIMESTAMP,
                   **({"awaiting_ssn": False} if skip_ein_now else {})}, merge=True)
    record_state(order_ref, "intake_complete")

    updated_order = order_ref.get().to_dict()
    if APP_ENV == "staging":
        send_admin_sms(
            f"🧪 [TEST] Signup complete! {updated_order.get('business_name', '')} - "
            f"{updated_order.get('first_name', '')} {updated_order.get('last_name', '')}. "
            "Staging only - do not file."
        )
    else:
        send_admin_sms(
            f"📋 [LIVE] Signup complete! {updated_order.get('business_name', '')} - "
            f"{updated_order.get('first_name', '')} {updated_order.get('last_name', '')} finished all steps. "
            f"Ready to file. Admin: app.launchbridge.ai/admin"
        )

    # Asset generation starts immediately on intake completion — brand kit,
    # website, docs, and Stripe Connect don't require the SSN or EIN. Name
    # is already cleared back in Step 3, so unlike the old single-page
    # intake form this no longer also needs to kick off run_name_check.
    background_tasks.add_task(run_early_assets, order_id)

    # Filing pipeline (SCC filing) still waits for the SSN, which is needed
    # for the EIN step that follows LLC approval.
    if not updated_order.get("awaiting_ssn") and updated_order.get("skip_llc_formation"):
        trigger_assets = advance_past_filing_confirmed(order_ref, updated_order)
        if trigger_assets:
            background_tasks.add_task(run_asset_generation, order_id)

    # Sends the customer to Step 6 (website customization) - previously
    # this redirected straight to the status dashboard, silently skipping
    # it for every customer despite the wizard's own step indicator
    # advertising a "Website" step. Asset generation (run_early_assets,
    # just above) already runs independently of whether/when this page is
    # visited - dashboard_website_submit redeploys with these preferences
    # if early assets already finished by the time the customer submits,
    # otherwise run_early_assets picks the fields up itself once it runs.
    return RedirectResponse(url=f"/dashboard/orders/{order_id}/website?ga_event=intake_complete", status_code=303)

# ── Step 6: website customization (optional) ────────────────────────────────

_STEP6_WEBSITE_SIMPLE_FIELDS = [
    "website_template", "website_tagline", "website_description",
    "service_1_name", "service_1_desc", "service_2_name", "service_2_desc",
    "service_3_name", "service_3_desc",
    "business_hours", "instagram_url", "facebook_url", "tiktok_url", "linkedin_url",
    "color_preference", "custom_primary_color", "backdrop_image_choice",
    "website_contact_phone", "website_contact_email", "website_contact_address",
]

@app.get("/dashboard/orders/{order_id}/website", response_class=HTMLResponse)
async def dashboard_website(request: Request, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    if not reached(order.get("state", "draft"), "name_selected"):
        return RedirectResponse(url=f"/dashboard/orders/{order_id}/name", status_code=303)

    # Generated once per order and cached on Firestore - otherwise every
    # page load (or a customer hitting back/forward) would burn a fresh
    # Gemini call and, worse, keep reshuffling the candidate grid out from
    # under whatever the customer already clicked.
    backdrop_options = order.get("backdrop_image_options")
    if not backdrop_options:
        try:
            backdrop_options = get_backdrop_image_options(order.get("business_idea", ""))
            order_ref.set({"backdrop_image_options": backdrop_options}, merge=True)
        except Exception as e:
            print(f"⚠️ Could not generate backdrop image options for order {order_id}: {e}")
            backdrop_options = []

    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    return templates.TemplateResponse(request, "dashboard_website.html", {
        "order_id": order_id,
        "business_name": order.get("business_name", ""),
        "order": order,
        "backdrop_image_options": backdrop_options,
        "csrf_token": make_csrf_token(session_id),
        "website_errors": {},
    })

@app.post("/dashboard/orders/{order_id}/website", response_class=HTMLResponse)
async def dashboard_website_submit(
    request: Request, background_tasks: BackgroundTasks,
    owned: tuple = Depends(get_owned_order),
):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")

    form_raw = await request.form()

    photo_errors = {}
    photo_data = {}
    for i in (1, 2, 3):
        upload = form_raw.get(f"photo_{i}")
        if upload is not None and getattr(upload, "filename", ""):
            raw = await upload.read()
            if len(raw) > MAX_UPLOAD_BYTES:
                photo_errors[f"photo_{i}"] = f"Photo {i} is over 5MB - please upload a smaller file."
            else:
                try:
                    photo_data[f"photo_{i}_data"] = process_photo(raw)
                except Exception:
                    photo_errors[f"photo_{i}"] = f"Could not process photo {i} - try a different file."

    form = {k: v for k, v in form_raw.items() if not (k.startswith("photo_") and hasattr(v, "filename"))}

    if not verify_csrf_token(session_id, (form.get("csrf_token") or "").strip()):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    if photo_errors:
        return templates.TemplateResponse(request, "dashboard_website.html", {
            "order_id": order_id,
            "business_name": order.get("business_name", ""),
            "order": {**order, **form},
            "backdrop_image_options": order.get("backdrop_image_options") or [],
            "csrf_token": make_csrf_token(session_id),
            "website_errors": photo_errors,
        }, status_code=400)

    safe_fields = {k: form[k] for k in _STEP6_WEBSITE_SIMPLE_FIELDS if form.get(k)}
    # Explicit bool rather than the truthy-only filter above: an unchecked
    # checkbox sends no form field at all, so this is the only way to let a
    # customer who opted in turn contact info back off again.
    safe_fields["website_contact_show"] = bool(form.get("website_contact_show"))
    order_ref.set({**safe_fields, **photo_data}, merge=True)

    # If run_early_assets already deployed a default site, redeploy with
    # these preferences instead of leaving the default in place - otherwise
    # (still generating, or Step 6 submitted before Step 5) run_early_assets
    # itself will pick these fields up whenever it runs/finishes.
    if order.get("website_url"):
        background_tasks.add_task(run_website_regeneration, order_id)

    return RedirectResponse(url=f"/dashboard/orders/{order_id}/complete", status_code=303)

@app.get("/dashboard/orders/{order_id}/complete", response_class=HTMLResponse)
async def dashboard_wizard_complete(request: Request, owned: tuple = Depends(get_owned_order)):
    """Shown once after Step 6 (whether submitted or skipped) - the wizard
    itself is done at this point, but asset generation is still running in
    the background, so this buys a beat before sending the customer to
    their (still-populating) order dashboard and reminds them the magic
    link in their inbox is the durable way back in, not just this session
    cookie."""
    return templates.TemplateResponse(request, "wizard_complete_interstitial.html", {
        "base_url": DASHBOARD_BASE_URL,
    })

# ── Auto-save (Steps 4-6) ────────────────────────────────────────────────────

_AUTOSAVE_ALLOWED_FIELDS = set(
    _STEP4_DETAILS_SIMPLE_FIELDS + _STEP5_BUSINESS_SIMPLE_FIELDS + _STEP6_WEBSITE_SIMPLE_FIELDS
)

@app.post("/dashboard/orders/{order_id}/autosave", response_class=HTMLResponse)
async def dashboard_autosave(request: Request, owned: tuple = Depends(get_owned_order)):
    """Fires on change/1s-debounced-keyup from any field on Steps 4-6 (see
    _wizard_progress.html siblings) so a customer who navigates away and
    comes back finds their in-progress answers pre-filled. Whitelisted
    against exactly the fields each step's real submit handler already
    accepts - autosave must never let the client write an arbitrary
    Firestore field."""
    order_ref, order, customer_id = owned
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")

    form = await request.form()
    if not verify_csrf_token(session_id, (form.get("csrf_token") or "").strip()):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    field = (form.get("field") or "").strip()
    if field not in _AUTOSAVE_ALLOWED_FIELDS:
        raise HTTPException(status_code=400, detail="Field not autosavable")

    # hx-include="this" sends the field's own name as the key (e.g.
    # address=123 Main St), not a literal "value" key - form.get("value")
    # was always empty, so every autosave call silently wrote "" over
    # whatever the customer had just typed, on every keystroke pause.
    order_ref.set({field: form.get(field, "")}, merge=True)
    return templates.TemplateResponse(request, "_autosave_indicator.html", {"field": field})


@app.post("/dashboard/orders/{order_id}/category-confirm", response_class=HTMLResponse)
async def dashboard_category_confirm(request: Request, owned: tuple = Depends(get_owned_order)):
    """Step 5's one-tap category confirmation (see dashboard_business.html)
    - fires on either the "That's right" button or picking a different
    option from the dropdown, both of which land here with whatever
    category should now be final. Always sets category_source to
    "confirmed", whether the value matches the AI's original suggestion
    or the customer picked something else - either way a human has now
    reviewed it, which is what category_source is tracking."""
    order_ref, order, customer_id = owned
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")

    form = await request.form()
    if not verify_csrf_token(session_id, (form.get("csrf_token") or "").strip()):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    category = (form.get("category") or "").strip()
    if category not in CATEGORY_TAXONOMY:
        raise HTTPException(status_code=400, detail="Not a valid category")

    order_ref.set({"category": category, "category_source": "confirmed"}, merge=True)
    return templates.TemplateResponse(request, "_autosave_indicator.html", {"field": "category"})


@app.get("/dashboard/orders/{order_id}/timeline", response_class=HTMLResponse)
async def dashboard_order_timeline(request: Request, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    order = ensure_payment_link(order_ref, order)
    return templates.TemplateResponse(request, "status_timeline.html", {
        **status_context(order_id, order),
        "onboarding_url": f"/connect/onboard/{order_id}" if order.get("stripe_connect_account_id") else None,
    })

@app.get("/dashboard/orders/{order_id}/download-website")
async def dashboard_download_website(request: Request, owned: tuple = Depends(get_owned_order)):
    order_ref, order, _ = owned
    order_id = order_ref.id
    # get_website_html reads back whatever's currently in GCS, which a
    # not-yet-approved draft (see run_website_generation) would have
    # overwritten too - gate on website_url (only set once an admin
    # approves, see run_website_approval) so an unapproved draft can never
    # be fetched by guessing this URL, matching the two UI surfaces that
    # link here (both already only render once state == "complete").
    if not order.get("website_url"):
        raise HTTPException(status_code=404, detail="Website file not found.")
    html = get_website_html(order_id)
    if not html:
        raise HTTPException(status_code=404, detail="Website file not found.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html)
    buf.seek(0)
    slug = re.sub(r"[^a-z0-9]+", "-", order.get("business_name", "website").lower()).strip("-")
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}-website.zip"'},
    )

DOCUMENT_LABELS = {
    "certificate": "Certificate of Organization",
    "ein_letter": "EIN Confirmation Letter (CP575)",
    "articles": "Articles of Organization",
    "operating_agreement": "Operating Agreement",
    "brand_kit": "Brand Kit",
    "logo": "Logo (SVG)",
    "marketing_plan": "Starter Marketing Plan (PDF)",
}

# Orders uploaded before document_store.py existed have no "documents"
# map at all - these are the deterministic legacy paths the old
# storage_service.py module always used (since removed), kept readable
# here (signed URLs work on any object regardless of who uploaded it)
# rather than re-uploading old files.
LEGACY_DOCUMENT_FALLBACK = {
    "certificate": ("certificate_uploaded_at", lambda order_id: f"orders/{order_id}/certificate.pdf"),
    "ein_letter": ("ein_letter_uploaded_at", lambda order_id: f"orders/{order_id}/ein_confirmation.pdf"),
}

def _document_object_name(order: dict, order_id: str, doc_id: str) -> str | None:
    """None means "not ready yet", not an error - callers turn that into
    a clean 404 rather than a 500."""
    documents = order.get("documents") or {}
    doc_info = documents.get(doc_id)
    if doc_info and doc_info.get("object_name"):
        return doc_info["object_name"]
    if doc_id in LEGACY_DOCUMENT_FALLBACK:
        flag_field, legacy_path_fn = LEGACY_DOCUMENT_FALLBACK[doc_id]
        if order.get(flag_field):
            return legacy_path_fn(order_id)
    return None

def _check_document_access_anomaly(session_id: str, customer_id: str):
    """A single session_id should only ever be associated with one
    customer_id - sessions are created for exactly one customer and never
    reassigned. If the access log shows otherwise (forged cookie,
    tampered Firestore record), that's worth an immediate page rather
    than staying silent until someone notices missing documents."""
    distinct_customers = {customer_id}
    for doc in DOCUMENT_ACCESS_LOG.where(filter=firestore.FieldFilter("session_id", "==", session_id)).limit(20).stream():
        distinct_customers.add(doc.to_dict().get("customer_id"))
    if len(distinct_customers) > 1:
        send_admin_sms(f"⚠️ SECURITY: session {session_id[:8]}... accessed documents as multiple customers: {distinct_customers}")

# Articles of Organization and Operating Agreement downloads removed -
# those two are emailed to customers instead (see dashboard_order.html's
# email-notice block). Every other document type (certificate, EIN
# letter, brand kit, logo) still downloads normally through this route.
_EMAIL_ONLY_DOCUMENT_IDS = {"articles", "operating_agreement"}

@app.get("/orders/{order_id}/documents/{doc_id}")
async def get_order_document(request: Request, doc_id: str, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id

    if doc_id not in DOCUMENT_LABELS or doc_id in _EMAIL_ONLY_DOCUMENT_IDS:
        raise HTTPException(status_code=404)

    object_name = _document_object_name(order, order_id, doc_id)
    if not object_name:
        raise HTTPException(status_code=404)

    url = generate_signed_url(object_name)

    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    DOCUMENT_ACCESS_LOG.add({
        "customer_id": customer_id, "doc_id": doc_id, "order_id": order_id,
        "session_id": session_id, "at": firestore.SERVER_TIMESTAMP,
    })
    _check_document_access_anomaly(session_id, customer_id)

    # Never logged - this redirect is the only place the signed URL
    # itself ever exists outside of document_store.generate_signed_url.
    return RedirectResponse(url=url, status_code=302)

@app.get("/cancel")
async def cancel():
    return RedirectResponse(url="/?cancelled=1")

@app.get("/connect/onboard/{order_id}")
async def connect_onboard(owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    if not order.get("stripe_connect_account_id"):
        return RedirectResponse(url="/dashboard/orders")

    # Not request.base_url - Cloud Run sits behind a proxy that terminates
    # TLS, so Starlette sees the connection as plain http:// even though
    # the customer's browser is on https://, and Stripe's live mode hard-
    # rejects an http:// redirect URL ("Livemode requests must always be
    # redirected via HTTPS"). Hardcoding the real public origin avoids
    # depending on whether the proxy forwards X-Forwarded-Proto.
    base_url = "https://app.launchbridge.ai"
    url = create_account_link(
        order["stripe_connect_account_id"],
        refresh_url=f"{base_url}/connect/onboard/{order_id}",
        return_url=f"{base_url}/dashboard/orders/{order_id}",
    )
    return RedirectResponse(url=url)

_TEST_EMAIL_MARKERS = ("test", "example", "e2e")

# Our own domains - a real customer would never sign up with an email on
# the company's own domain, so any order using one is a teammate's
# internal walkthrough, not a sale.
_INTERNAL_EMAIL_DOMAINS = ("launchbridge.ai", "teamlaunchbridge.com")

def _is_test_order(order: dict) -> bool:
    """True for orders the admin dashboard should file under "Test Orders"
    rather than "Live Orders": never-completed checkouts (draft),
    payments that failed outright, anything with a giveaway email
    (our own e2e/test-script runs, *@example.com placeholders, or an
    address on one of our own internal domains), or an order that has
    paid_at but no checkout_at - structurally impossible through the real
    /start flow (checkout_at is always written when the order doc is
    first created, before Stripe can ever confirm payment), so it can
    only be a script that wrote paid_at directly to Firestore without
    going through checkout - regardless of state, since a test script can
    still walk an order all the way to "complete"."""
    if order.get("state") in ("draft", "payment_failed"):
        return True
    if order.get("paid_at") and not order.get("checkout_at"):
        return True
    email = (order.get("email") or "").lower()
    if any(email.endswith("@" + domain) for domain in _INTERNAL_EMAIL_DOMAINS):
        return True
    return any(marker in email for marker in _TEST_EMAIL_MARKERS)

def _get_page_view_stats() -> dict:
    """Reads the daily buckets track_page_view() writes on every homepage
    hit. Bounded reads (at most ~31 daily docs for the month figure) - fine
    for an admin page load, not a hot path. all_time comes from its own
    running-total doc rather than summing every daily bucket ever written,
    so this stays cheap indefinitely as the site accumulates history."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - datetime.timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)

    def _count_for(date_str: str) -> int:
        doc = db.collection(PAGE_VIEWS_COLLECTION).document(date_str).get()
        return (doc.to_dict() or {}).get("count", 0) if doc.exists else 0

    def _sum_since(start: datetime.datetime) -> int:
        days = (today_start - start).days + 1
        return sum(_count_for((start + datetime.timedelta(days=i)).strftime("%Y-%m-%d")) for i in range(days))

    total_doc = db.collection(PAGE_VIEWS_COLLECTION).document("_total").get()
    return {
        "visits_today": _count_for(today_start.strftime("%Y-%m-%d")),
        "visits_week": _sum_since(week_start),
        "visits_month": _sum_since(month_start),
        "visits_total": (total_doc.to_dict() or {}).get("count", 0) if total_doc.exists else 0,
    }

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, authorized: bool = Depends(verify_admin)):
    orders = []
    query = ORDERS.order_by("created_at", direction=firestore.Query.DESCENDING)
    for doc in query.stream():
        order = doc.to_dict()
        order["id"] = doc.id
        order["step_label"] = step_label(order)
        orders.append(order)

    # Every order lands in exactly one tab: test_orders is the actual
    # predicate, live_orders is everything else - not two independently
    # overlapping filters - so the two counts always add up to len(orders).
    test_orders = [o for o in orders if _is_test_order(o)]
    live_orders = [o for o in orders if not _is_test_order(o)]

    # ── Analytics stats from Firestore ────────────────────────────────────
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - datetime.timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)

    def _ts(v):
        if v is None:
            return None
        if hasattr(v, "tzinfo"):
            return v if v.tzinfo else v.replace(tzinfo=datetime.timezone.utc)
        return None

    # "Orders" and "Revenue" only count orders that actually paid - a
    # started-but-abandoned checkout (draft) shouldn't inflate either
    # figure. Both also exclude test orders (see _is_test_order) so
    # internal test runs don't pollute what's meant to be real sales data.
    # The funnel intentionally keeps every non-test order regardless of
    # stage reached - that's the whole point of a funnel - but still
    # excludes test orders for the same reason.
    successful_orders = [o for o in live_orders if o.get("paid_at")]
    stats = {
        "orders_today": sum(1 for o in successful_orders if _ts(o.get("paid_at")) >= today_start),
        "orders_week": sum(1 for o in successful_orders if _ts(o.get("paid_at")) >= week_start),
        "orders_month": sum(1 for o in successful_orders if _ts(o.get("paid_at")) >= month_start),
        "revenue_today": sum((250 if o.get("founding_member") else 350) for o in successful_orders if _ts(o.get("paid_at")) >= today_start),
        "revenue_week": sum((250 if o.get("founding_member") else 350) for o in successful_orders if _ts(o.get("paid_at")) >= week_start),
        "revenue_month": sum((250 if o.get("founding_member") else 350) for o in successful_orders if _ts(o.get("paid_at")) >= month_start),
        "funnel_checkout": sum(1 for o in live_orders if o.get("checkout_at")),
        "funnel_paid": sum(1 for o in live_orders if o.get("paid_at")),
        "funnel_intake": sum(1 for o in live_orders if o.get("intake_complete_at")),
        "funnel_assets": sum(1 for o in live_orders if o.get("assets_status") == "complete"),
        "funnel_complete": sum(1 for o in live_orders if o.get("state") == "complete"),
        **_get_page_view_stats(),
    }

    irs_open = is_irs_open()
    irs_next_window_eta = None if irs_open else format_eta(next_irs_open())

    for order in orders:
        if order.get("next_available_window"):
            order["next_available_window_eta"] = format_eta(datetime.datetime.fromisoformat(order["next_available_window"]))

    def fmt_datetime(ts) -> str:
        return ts.strftime("%B %-d, %Y %I:%M %p").replace(" 0", " ") if ts else None

    # Gmail poller removed - SCC approval emails go directly to customers
    # Admin manually marks LLC as approved in the admin dashboard

    # Filtered in Python rather than a Firestore .where("resolved", "==",
    # False).order_by("timestamp", ...) query - that combination needs a
    # composite index that doesn't exist for this brand-new collection, and
    # error volume is low enough that fetching a recent window and
    # filtering here is simpler than provisioning one.
    recent_errors = []
    error_query = ERRORS.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(50)
    for doc in error_query.stream():
        entry = doc.to_dict()
        if entry.get("resolved"):
            continue
        # Staging shares this collection with production (see app_env tag
        # added where errors are logged) - the admin only cares about real
        # customer-facing errors. Entries logged before this tag existed
        # have no app_env at all; treat those as production so old errors
        # don't silently vanish from the dashboard.
        if entry.get("app_env", "production") != "production":
            continue
        entry["id"] = doc.id
        entry["timestamp_display"] = fmt_datetime(entry.get("timestamp"))
        recent_errors.append(entry)
        if len(recent_errors) >= 25:
            break

    return templates.TemplateResponse(request, "admin.html", {
        "orders": orders,
        "live_orders": live_orders,
        "test_orders": test_orders,
        "irs_open": irs_open,
        "irs_next_window_eta": irs_next_window_eta,
        "warning": request.query_params.get("warning"),
        "stats": stats,
        "recent_errors": recent_errors,
    })

@app.post("/admin/errors/{error_id}/resolve")
async def admin_resolve_error(error_id: str, authorized: bool = Depends(verify_admin)):
    ERRORS.document(error_id).set({"resolved": True, "resolved_at": firestore.SERVER_TIMESTAMP}, merge=True)
    return RedirectResponse(url="/admin", status_code=303)

def _fmt_step_event(event: dict) -> dict:
    """Shapes one agent_events doc for display - both the live feed and the
    per-run timeline use this, so formatting (time, duration, model badge)
    never drifts between the two views."""
    ts = event.get("timestamp", "")
    time_label = ""
    if isinstance(ts, str) and len(ts) >= 16:
        try:
            time_label = datetime.datetime.fromisoformat(ts).strftime("%H:%M:%S")
        except ValueError:
            time_label = ts[11:16]
    latency = event.get("latency_ms")
    latency_label = f"{latency / 1000:.1f}s" if isinstance(latency, (int, float)) else ""
    return {
        **event,
        "time_label": time_label,
        "latency_label": latency_label,
        "order_short": (event.get("order_ref") or "")[:8],
    }

@app.get("/admin/ai-ops", response_class=HTMLResponse)
async def admin_ai_ops(request: Request, authorized: bool = Depends(verify_admin)):
    """AI Operations Evidence Layer dashboard (design doc §4.11, Phase 1) -
    read-only: aggregate KPIs (from daily_metrics), a live activity feed,
    and recent runs to drill into (see admin_ai_ops_run below). Same admin
    auth as the rest of /admin, nothing new to configure.

    KPI totals are summed in Python over daily_metrics docs (one per day)
    rather than a Firestore aggregation query - this collection stays small
    (one doc/day) for years, so streaming and summing it here is simpler
    than provisioning/reasoning about aggregation query semantics for what
    is, in practice, a tiny read."""
    totals = {
        "orders_processed": 0, "total_steps": 0, "autonomous_steps": 0,
        "failed_steps": 0, "gemini_calls": 0, "tokens_in": 0, "tokens_out": 0,
        "cost_estimate_usd": 0.0, "human_touch_minutes": 0.0, "founder_minutes_saved": 0.0,
    }
    for doc in DAILY_METRICS.stream():
        d = doc.to_dict() or {}
        for key in totals:
            totals[key] += d.get(key) or 0

    autonomy_ratio = (totals["autonomous_steps"] / totals["total_steps"]) if totals["total_steps"] else 0.0
    avg_human_minutes = (totals["human_touch_minutes"] / totals["orders_processed"]) if totals["orders_processed"] else 0.0

    feed = []
    for doc in AGENT_EVENTS.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(50).stream():
        event = doc.to_dict() or {}
        if event.get("phase") not in ("completed", "failed"):
            continue
        feed.append(_fmt_step_event(event))

    recent_runs = []
    for doc in ORDER_RUNS.order_by("last_event_at", direction=firestore.Query.DESCENDING).limit(20).stream():
        run = doc.to_dict() or {}
        run["run_id"] = doc.id
        run["display_status"] = "error" if run.get("failed_steps") else ("complete" if run.get("completed_at") else "in_progress")
        recent_runs.append(run)

    return templates.TemplateResponse(request, "admin_ai_ops.html", {
        "totals": totals,
        "autonomy_ratio": autonomy_ratio,
        "avg_human_minutes": avg_human_minutes,
        "founder_hours_saved": totals["founder_minutes_saved"] / 60.0,
        "feed": feed,
        "recent_runs": recent_runs,
        "manual_baseline_min": ai_ops.MANUAL_BASELINE_MIN,
    })

@app.get("/admin/ai-ops/runs/{run_id}", response_class=HTMLResponse)
async def admin_ai_ops_run(request: Request, run_id: str, authorized: bool = Depends(verify_admin)):
    """Per-order run timeline - every agent_events doc for one run_id, in
    order. This is the "real, exportable evidence that AI runs the
    business" view for a single order: every instrumented step it went
    through, which were autonomous vs human-touched, how long each took."""
    run_doc = ORDER_RUNS.document(run_id).get()
    run = run_doc.to_dict() if run_doc.exists else None
    if run:
        run["run_id"] = run_id
        run["display_status"] = "error" if run.get("failed_steps") else ("complete" if run.get("completed_at") else "in_progress")

    # Single equality filter, sorted in Python rather than adding
    # .order_by("timestamp") - that combination needs a composite index
    # that doesn't exist for this collection (same tradeoff as
    # admin_dashboard's recent_errors above), and a run's event count is
    # small enough (~13 steps) that sorting in memory is trivial.
    raw_events = [doc.to_dict() or {} for doc in AGENT_EVENTS.where("run_id", "==", run_id).stream()]
    raw_events.sort(key=lambda e: (e.get("timestamp") or "", e.get("seq") or 0))
    events = [_fmt_step_event(e) for e in raw_events]

    return templates.TemplateResponse(request, "admin_ai_ops_run.html", {
        "run_id": run_id,
        "run": run,
        "events": events,
    })

@app.get("/admin/ai-ops/lookup", response_class=HTMLResponse)
async def admin_ai_ops_lookup(request: Request, order_id: str = "", authorized: bool = Depends(verify_admin)):
    """Convenience redirect for the admin's own order card, which knows an
    order_id but not its run_id - looks it up once and forwards to the
    canonical per-run timeline."""
    order = ORDERS.document(order_id).get().to_dict() if order_id else None
    if not order or not order.get("run_id"):
        return RedirectResponse(url="/admin/ai-ops", status_code=303)
    return RedirectResponse(url=f"/admin/ai-ops/runs/{order['run_id']}", status_code=303)

def _approve_and_trigger_filing(order_ref, order_id: str, background_tasks: BackgroundTasks) -> None:
    """Shared by both the "name verified" and "admin manually confirmed"
    approve paths below. Kicks off filing + asset generation in parallel -
    run_early_assets is idempotent (skips whatever the customer's intake
    already triggered), a safety net for the rare case assets haven't run
    yet."""
    order = order_ref.get().to_dict() or {}
    ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))
    ai_ops.emit_event(
        "human_review", "completed", step_label="Admin Approve → File with SCC",
        operation="review", autonomy="human_only", actor="human", status="success",
        output_summary="admin approved order for SCC filing", human_touched=True,
        human_action="approve",
    )
    record_state(order_ref, "review_approved", review_approved_at=firestore.SERVER_TIMESTAMP)
    background_tasks.add_task(run_scc_filing, order_id)
    background_tasks.add_task(run_early_assets, order_id)

@app.get("/admin/{order_id}/approve-button", response_class=HTMLResponse)
async def admin_approve_button(request: Request, order_id: str, authorized: bool = Depends(verify_admin)):
    """Re-renders the plain Approve button - the "Cancel" action on the
    "could not auto-verify" prompt (see _admin_approve_result.html)."""
    return templates.TemplateResponse(request, "_admin_approve_button.html", {"order_id": order_id})

@app.post("/admin/{order_id}/verify-and-approve", response_class=HTMLResponse)
async def admin_verify_and_approve(request: Request, order_id: str, background_tasks: BackgroundTasks, authorized: bool = Depends(verify_admin)):
    """The Approve button's real target now: synchronously (blocking this
    one admin request - a Playwright/CDP round trip takes real time, which
    is fine for a single manual click) re-verifies the name on Virginia
    SCC via a real browser before ever kicking off the filing wizard. This
    is on top of, not instead of, file_llc_on_scc's own pre-check
    (verify_name_before_filing) and its Step 3 in-wizard check - defense
    in depth per Part 5's "safety net" framing, not redundant busywork:
    catching a rejection here means the customer finds out in seconds
    instead of however long the filing wizard takes to fail."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return templates.TemplateResponse(request, "_admin_approve_button.html", {"order_id": order_id})

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, verify_name_before_filing, order.get("business_name", ""))
    print(f"[admin-approve] verify_name_before_filing for {order_id}: {result}")

    if result["available"] is True:
        _approve_and_trigger_filing(order_ref, order_id, background_tasks)
        return templates.TemplateResponse(request, "_admin_approve_result.html", {"order_id": order_id, "status": "verified"})

    if result["available"] is False:
        handle_name_rejected(order_ref, order, order_id, message=result.get("message", ""))
        return templates.TemplateResponse(request, "_admin_approve_result.html", {"order_id": order_id, "status": "taken"})

    return templates.TemplateResponse(request, "_admin_approve_result.html", {"order_id": order_id, "status": "unknown"})

@app.post("/admin/{order_id}/confirm-approve", response_class=HTMLResponse)
async def admin_confirm_approve(request: Request, order_id: str, background_tasks: BackgroundTasks, authorized: bool = Depends(verify_admin)):
    """"Yes, proceed" on the "could not auto-verify" prompt - the admin has
    manually checked Virginia SCC themselves (per the prompt's own
    instruction) and is overriding the inconclusive automated result."""
    order_ref = ORDERS.document(order_id)
    if not order_ref.get().exists:
        return templates.TemplateResponse(request, "_admin_approve_button.html", {"order_id": order_id})
    _approve_and_trigger_filing(order_ref, order_id, background_tasks)
    return templates.TemplateResponse(request, "_admin_approve_result.html", {"order_id": order_id, "status": "confirmed_manually"})

@app.post("/admin/{order_id}/mark-filed")
async def admin_mark_filed(order_id: str, background_tasks: BackgroundTasks, scc_confirmation_number: str = Form(...), authorized: bool = Depends(verify_admin)):
    scc_confirmation_number = scc_confirmation_number.strip()
    if not scc_confirmation_number:
        return RedirectResponse(url=f"/admin?warning={quote('SCC confirmation number is required to mark an order as approved.')}", status_code=303)

    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return RedirectResponse(url="/admin", status_code=303)

    order_ref.set({"scc_confirmation_number": scc_confirmation_number}, merge=True)
    send_llc_approved_email(order, order_id, scc_confirmation_number)

    trigger_assets = advance_past_filing_confirmed(order_ref, order)
    if trigger_assets:
        background_tasks.add_task(run_asset_generation, order_id)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/{order_id}/apply-ein")
async def admin_apply_ein(order_id: str, background_tasks: BackgroundTasks, authorized: bool = Depends(verify_admin)):
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order or order.get("state") not in EIN_ELIGIBLE_STATES:
        return RedirectResponse(url="/admin", status_code=303)

    if not is_irs_open():
        window = next_irs_open()
        order_ref.set({"ein_status": "queued", "next_available_window": window.isoformat()}, merge=True)
        warning = f"Apply for EIN was attempted outside IRS hours (Mon-Fri 7am-10pm ET) - queued instead. Opens {format_eta(window)}."
        return RedirectResponse(url=f"/admin?warning={quote(warning)}", status_code=303)

    background_tasks.add_task(run_ein_filing, order_id)
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/ein-filing-link/{order_id}", response_class=HTMLResponse)
async def admin_ein_filing_link(order_id: str, background_tasks: BackgroundTasks, token: str = ""):
    """The one-click link sent by notify_ein_ready's admin email - lets
    filing start straight from a phone without first logging into the
    dashboard. The signed token (see _make_ein_filing_link_token) is the
    auth here instead of verify_admin/HTTP Basic, so this route deliberately
    has no Depends(verify_admin)."""
    if not _verify_ein_filing_link_token(order_id, token):
        raise HTTPException(status_code=404, detail="Not found")

    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order or order.get("state") not in EIN_ELIGIBLE_STATES:
        return HTMLResponse("<p>This order is no longer ready for EIN filing - check the admin dashboard.</p>")

    if order.get("ein_submitted_to_irs"):
        return HTMLResponse("<p>An EIN was already submitted for this order - check the admin dashboard, do not re-file.</p>")

    if not is_irs_open():
        window = next_irs_open()
        order_ref.set({"ein_status": "queued", "next_available_window": window.isoformat()}, merge=True)
        return HTMLResponse(f"<p>IRS is currently closed - filing has been queued and will run automatically when it opens ({format_eta(window)}).</p>")

    background_tasks.add_task(run_ein_filing, order_id)
    return HTMLResponse(f"<p>✅ EIN filing started for {order.get('business_name', '')}. You'll get a text when it's done.</p>")

@app.get("/admin/{order_id}/view-dashboard")
async def admin_view_dashboard(order_id: str, authorized: bool = Depends(verify_admin)):
    """Admin-only "view as customer" shortcut - the customer dashboard
    has no direct admin bypass of its own (ownership is always checked
    via a real session), so this mints a fresh magic link for the
    order's email and immediately redirects the admin's own browser
    through it, the same one-click idea as admin_ein_filing_link above."""
    order = ORDERS.document(order_id).get().to_dict()
    if not order or not order.get("email"):
        return RedirectResponse(url="/admin")
    return RedirectResponse(url=create_magic_link(order["email"]))

@app.post("/admin/{order_id}/mark-ein")
async def admin_mark_ein(order_id: str, background_tasks: BackgroundTasks, ein: str = Form(...), authorized: bool = Depends(verify_admin)):
    """Manual fallback for the rare case run_ein_filing reports submitted
    but couldn't read the EIN back off the confirmation page - the admin
    types in the number recovered from the screenshot/IRS site/mail.
    Shares mark_ein_issued with the automatic success path, so the
    customer email/SMS/asset generation are identical either way."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if order:
        ai_ops.set_current_order(order_id, ai_ops.ensure_run_id(order_ref, order))
        ai_ops.emit_event(
            "human_review", "completed", step_label="Admin Manual EIN Entry",
            operation="review", autonomy="human_only", actor="human", status="success",
            output_summary="admin manually recorded EIN from IRS confirmation",
            human_touched=True, human_action="edit",
        )
        mark_ein_issued(order_ref, order, order_id, ein, background_tasks)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/{order_id}/retry-agents")
async def admin_retry_agents(order_id: str, background_tasks: BackgroundTasks, authorized: bool = Depends(verify_admin)):
    """Re-runs whichever asset steps haven't succeeded yet for an existing,
    already-paid order. run_early_assets covers brand kit, marketing plan,
    LLC docs, and Stripe Connect — all idempotent, so it skips anything
    already done. The website is separate - see /admin/{order_id}/
    generate-website - not retried here. If the EIN has already been
    issued, also retries run_asset_generation (payment link + state
    advancement)."""
    order_ref = ORDERS.document(order_id)
    order_snap = order_ref.get()
    if not order_snap.exists:
        return RedirectResponse(url="/admin", status_code=303)

    background_tasks.add_task(run_early_assets, order_id)
    if reached(order_snap.to_dict().get("state", "draft"), "ein_issued"):
        background_tasks.add_task(run_asset_generation, order_id)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/{order_id}/generate-website", response_class=HTMLResponse)
async def admin_generate_website(request: Request, order_id: str, authorized: bool = Depends(verify_admin)):
    """First-generation (or pre-approval "Regenerate") of an order's
    website - see run_website_generation. Always re-renders the same
    review-panel partial the admin's button lives in, whether this attempt
    succeeded or failed, so the admin sees the fresh draft/preview (or the
    error) in place immediately.

    Synchronous (blocking this one admin request), same as the SCC
    verify-and-approve button - a real Gemini call plus a Firebase deploy
    takes real time, which is fine for a single manual click and is the
    only way to show the admin the actual draft (or actual error) instead
    of firing a background task and hoping they refresh later."""
    order_ref = ORDERS.document(order_id)
    if not order_ref.get().exists:
        return HTMLResponse("Order not found.", status_code=404)

    run_website_generation(order_id)
    order = order_ref.get().to_dict()
    order["id"] = order_id
    return templates.TemplateResponse(request, "_admin_website_review.html", {"order": order})

@app.post("/admin/{order_id}/approve-website", response_class=HTMLResponse)
async def admin_approve_website(request: Request, order_id: str, authorized: bool = Depends(verify_admin)):
    """Publishes the reviewed draft - see run_website_approval. The only
    place a generated website actually becomes visible to the customer.
    Re-renders the same review-panel partial, now showing the live state."""
    order_ref = ORDERS.document(order_id)
    if not order_ref.get().exists:
        return HTMLResponse("Order not found.", status_code=404)

    run_website_approval(order_id)
    order = order_ref.get().to_dict()
    order["id"] = order_id
    return templates.TemplateResponse(request, "_admin_website_review.html", {"order": order})

@app.post("/admin/{order_id}/regenerate-website", response_class=HTMLResponse)
async def admin_regenerate_website(request: Request, order_id: str, authorized: bool = Depends(verify_admin)):
    """Force-redeploys just the website, straight back to live, for an
    order whose website was already approved - for when the admin isn't
    happy with the live site and wants a fresh attempt without going
    through review again. Never touches Stripe Connect/payment link.

    Synchronous (blocking this one admin request), same as the SCC
    verify-and-approve button - a real Gemini call plus a Firebase deploy
    takes real time, which is fine for a single manual click and is the
    only way to show the admin the actual new URL (or actual error)
    instead of firing a background task and hoping they refresh later."""
    order_ref = ORDERS.document(order_id)
    if not order_ref.get().exists:
        return templates.TemplateResponse(request, "_admin_regenerate_result.html", {
            "order_id": order_id, "success": False, "error": "Order not found.",
        })

    result = run_website_regeneration(order_id)
    return templates.TemplateResponse(request, "_admin_regenerate_result.html", {
        "order_id": order_id, **result,
    })

@app.post("/admin/{order_id}/regenerate-logo", response_class=HTMLResponse)
async def admin_regenerate_logo(request: Request, order_id: str, authorized: bool = Depends(verify_admin)):
    """Regenerates just the logo (see run_logo_regeneration) - for orders
    whose brand_result predates this feature, or where the admin just
    wants a fresh attempt without paying for/waiting on a full content
    regeneration. Synchronous, same rationale as regenerate-website."""
    order_ref = ORDERS.document(order_id)
    if not order_ref.get().exists:
        return templates.TemplateResponse(request, "_admin_regenerate_logo_result.html", {
            "order_id": order_id, "success": False, "error": "Order not found.",
        })

    result = run_logo_regeneration(order_id)
    return templates.TemplateResponse(request, "_admin_regenerate_logo_result.html", {
        "order_id": order_id, **result,
    })

@app.get("/admin/{order_id}/stripe-check-button", response_class=HTMLResponse)
async def admin_stripe_check_button(request: Request, order_id: str, authorized: bool = Depends(verify_admin)):
    return templates.TemplateResponse(request, "_admin_stripe_check_button.html", {"order_id": order_id})

@app.post("/admin/{order_id}/check-stripe-status", response_class=HTMLResponse)
async def admin_check_stripe_status(request: Request, order_id: str, authorized: bool = Depends(verify_admin)):
    """Manual trigger for check_and_update_website - same idempotent
    Stripe-check-then-regenerate as the hourly stripe_activation_scheduler,
    just synchronous so the admin sees the real result immediately instead
    of waiting up to an hour."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return templates.TemplateResponse(request, "_admin_stripe_check_result.html", {
            "order_id": order_id, "message": "❌ Order not found.",
        })

    result = check_and_update_website(order_id)
    if result.get("updated"):
        message = f"✅ Payments active - website updated: {result['url']}"
    elif not order.get("stripe_connect_account_id"):
        message = "⏳ No Stripe account yet for this order."
    elif order.get("payment_button_live"):
        message = "✅ Payments active - website updated"
    else:
        message = "⏳ Customer hasn't completed Stripe setup yet"
    return templates.TemplateResponse(request, "_admin_stripe_check_result.html", {
        "order_id": order_id, "message": message,
    })

@app.get("/admin/migrate-assets", response_class=HTMLResponse)
async def admin_migrate_assets(background_tasks: BackgroundTasks, authorized: bool = Depends(verify_admin)):
    """One-time migration: finds all paid orders that haven't had early
    asset generation run yet and triggers run_early_assets for each.
    Safe to call multiple times — run_early_assets is fully idempotent
    and skips any step that already has a result on file."""
    triggered = []
    already_done = []
    skipped_no_intake = []

    for doc in ORDERS.stream():
        order = doc.to_dict()
        order_id = doc.id
        state = order.get("state", "draft")
        if state == "draft":
            continue
        if order.get("early_assets_done") or order.get("assets_status") == "complete":
            already_done.append(order_id)
            continue
        if order.get("awaiting_intake"):
            skipped_no_intake.append(order_id)
            continue
        triggered.append(f"{order_id} ({order.get('business_name', '?')})")
        background_tasks.add_task(run_early_assets, order_id)

    html = (
        "<html><body style='font-family:sans-serif;padding:32px;'>"
        "<h2>Asset Migration</h2>"
        f"<p><strong>Triggered ({len(triggered)}):</strong> {', '.join(triggered) or 'none'}</p>"
        f"<p><strong>Already done ({len(already_done)}):</strong> {len(already_done)} orders skipped</p>"
        f"<p><strong>Skipped — awaiting intake ({len(skipped_no_intake)}):</strong> "
        f"{', '.join(skipped_no_intake) or 'none'}</p>"
        "<p><a href='/admin'>← Back to Admin</a></p>"
        "</body></html>"
    )
    return HTMLResponse(html)

@app.post("/admin/migrate-to-firebase", response_class=HTMLResponse)
async def admin_migrate_to_firebase(background_tasks: BackgroundTasks, authorized: bool = Depends(verify_admin)):
    """One-time migration: re-deploys all GitHub Pages sites to Firebase Hosting.
    Finds orders where website_url contains 'github.io', regenerates the HTML
    from stored content, and deploys to Firebase. Safe to re-run — skips orders
    already on Firebase (website_hosting == 'firebase')."""

    def _migrate_one(order_id: str, order: dict):
        business_name = order.get("business_name", "")
        content = order.get("website_content")
        template_name = order.get("website_template", "professional")
        if not content or not business_name:
            print(f"[migrate] Skipping {order_id} — missing content or business_name")
            return
        try:
            _site_id = make_site_id(business_name, order_id)
            html = render_website_html(
                content, business_name,
                template_name=template_name,
                payment_link_url=order.get("stripe_payment_link_url"),
                hours=order.get("business_hours"),
                instagram_url=order.get("instagram_url"),
                facebook_url=order.get("facebook_url"),
                tiktok_url=order.get("tiktok_url"),
                linkedin_url=order.get("linkedin_url"),
                order_id=order_id,
                site_url=f"https://{_site_id}.web.app",
                contact_phone=order.get("website_contact_phone") if order.get("website_contact_show") else None,
                contact_email=order.get("website_contact_email") if order.get("website_contact_show") else None,
                contact_address=order.get("website_contact_address") if order.get("website_contact_show") else None,
                logo_data_uri=order.get("logo_data_uri"),
                favicon_data_uri=order.get("favicon_data_uri"),
            )
            deployed = deploy_website(business_name, html, order_id=order_id)
            if deployed:
                print(f"[migrate] ✅ {order_id} ({business_name}) → {deployed['url']}")
            else:
                print(f"[migrate] ⚠️ {order_id} ({business_name}) — deploy failed")
        except Exception as e:
            print(f"[migrate] ⚠️ {order_id} ({business_name}) — crashed: {e}")

    triggered, skipped_firebase, skipped_no_site, skipped_no_content = [], [], [], []
    for doc in ORDERS.stream():
        order = doc.to_dict()
        order_id = doc.id
        if order.get("website_hosting") == "firebase":
            skipped_firebase.append(order_id)
            continue
        url = order.get("website_url", "")
        if not url:
            skipped_no_site.append(order_id)
            continue
        if not order.get("website_content"):
            skipped_no_content.append(order_id)
            continue
        triggered.append(f"{order_id} ({order.get('business_name', '?')})")
        background_tasks.add_task(_migrate_one, order_id, order)

    html = (
        "<html><body style='font-family:sans-serif;padding:32px;'>"
        "<h2>Firebase Hosting Migration</h2>"
        f"<p><strong>Migrating ({len(triggered)}):</strong> {', '.join(triggered) or 'none'}</p>"
        f"<p><strong>Already on Firebase ({len(skipped_firebase)}):</strong> {len(skipped_firebase)} skipped</p>"
        f"<p><strong>No site yet ({len(skipped_no_site)}):</strong> {', '.join(skipped_no_site) or 'none'}</p>"
        f"<p><strong>No stored content ({len(skipped_no_content)}):</strong> {', '.join(skipped_no_content) or 'none'}</p>"
        "<p>Check Cloud Run logs for per-order results.</p>"
        "<p><a href='/admin'>← Back to Admin</a></p>"
        "</body></html>"
    )
    return HTMLResponse(html)

@app.post("/contact", response_class=HTMLResponse)
async def contact(request: Request):
    """Two distinct callers share this one route: the authenticated
    dashboard's own "contact support" form (an htmx partial swap, identified
    by the HX-Request header) and the public contact form embedded on every
    deployed customer website (a plain, full-page <form> POST - no fetch, no
    JS, so no CORS preflight is ever involved even though those sites live
    on a different origin on Firebase Hosting). The two need different
    responses: htmx gets the small result partial it already targets,
    everyone else gets a real standalone page since their browser actually
    navigates here."""
    form = await request.form()
    order_id = (form.get("order_id") or "").strip()
    business_name = (form.get("business_name") or "").strip()
    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip()
    message = (form.get("message") or "").strip()
    is_htmx = request.headers.get("hx-request") == "true"
    back_url = request.headers.get("referer")

    # Only an authenticated dashboard session needs CSRF protection - a
    # public website visitor has no session to protect and no csrf_token
    # field at all, so it's left alone. This is the one route in the
    # rework where a real 403 is correct, since a CSRF mismatch is "stale
    # form," not an ownership question.
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    if session_id and verify_and_touch_session(session_id):
        if not verify_csrf_token(session_id, (form.get("csrf_token") or "").strip()):
            raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    if not (name and email and message):
        if is_htmx:
            return HTMLResponse('<p class="text-red-400 text-sm mt-2">Please fill in your name, email, and message.</p>', status_code=400)
        return templates.TemplateResponse(request, "contact_success.html", {
            "error": "Please fill in your name, email, and message.", "back_url": back_url,
            "business_name": business_name,
        }, status_code=400)

    db.collection("contact_messages").add({
        "order_id": order_id, "business_name": business_name, "name": name, "email": email, "message": message,
        "created_at": firestore.SERVER_TIMESTAMP,
    })

    send_visitor_message_email(business_name or "your business", name, email, message, SUPPORT_EMAIL)
    if not is_htmx and order_id:
        # A public website visitor's message also goes straight to the
        # business owner, not just our own support inbox - they're the one
        # who actually needs the lead. The dashboard's own contact-support
        # form (is_htmx) skips this since there the "visitor" is already
        # the order's owner messaging us, not a customer of theirs.
        order = ORDERS.document(order_id).get().to_dict()
        if order and order.get("email"):
            send_visitor_message_email(business_name or order.get("business_name", "your business"), name, email, message, order["email"])

    if is_htmx:
        return templates.TemplateResponse(request, "contact_result.html", {})
    return templates.TemplateResponse(request, "contact_success.html", {
        "back_url": back_url, "business_name": business_name,
    })

@app.get("/health")
def health():
    return {"status": "ok"}
