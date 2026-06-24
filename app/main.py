import os
import secrets
import hmac
import hashlib
import asyncio
import datetime
from urllib.parse import quote
from google.cloud import firestore
from fastapi import FastAPI, Request, BackgroundTasks, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from app.config import FIREBASE_PROJECT_ID, ADMIN_PASSWORD, ORDERS_COLLECTION, STATUS_SESSION_SECRET
from app.agents.name_agent import screen_business_name
from app.agents.name_check_agent import check_business_name
from app.agents.scc_name_check import check_name_on_scc, check_llc_exists_on_scc, SCC_NAME_CHECK_URL
from app.agents.llc_agent import generate_llc_paperwork
from app.agents.brand_agent import generate_brand_kit
from app.agents.marketing_agent import generate_marketing_plan
from app.agents.pdf_agent import generate_llc_pdf
from app.scc_llc_filer import file_llc_on_scc
from app.ein_filer import file_ein_with_irs
from app.utils.irs_hours import is_irs_open, next_irs_open, format_eta
from app.secrets import preload as preload_secrets
from app.agents.website_agent import generate_website
from app.deployer import deploy_website
from app.photo_utils import process_photo, MAX_UPLOAD_BYTES
from app.stripe_service import (
    create_checkout_session,
    retrieve_checkout_session,
    create_connect_account,
    create_account_link,
    create_pay_what_you_want_payment_link,
    is_account_active,
    construct_webhook_event,
)
from app.ssn_vault import (
    encrypt_ssn, decrypt_ssn, delete_ssn, ssn_age_hours, is_ssn_stored,
)
from app.log_scrub import scrub_ssn
from app.validators import validate_intake_form, validate_ssn, ALL_VALIDATED_FIELDS
from app.email_service import (
    send_order_received_email,
    send_documents_ready_email,
    send_llc_filed_email,
    send_llc_approved_email,
    send_ein_issued_email,
    send_ein_filing_ready_email,
    send_website_live_email,
    send_everything_complete_email,
    send_order_id_email,
    send_ssn_expired_email,
    send_magic_link_email,
)
from app.storage_service import fetch_certificate, fetch_ein_letter
from app.document_store import upload_document, generate_signed_url
from app.sms import send_admin_sms
from app.dashboard_auth import (
    create_magic_link, redeem_magic_link, create_session,
    verify_and_touch_session, delete_session, SESSION_ABSOLUTE_SECONDS,
)
from app.dashboard_security import SecurityHeadersMiddleware, make_csrf_token, verify_csrf_token

app = FastAPI(title="Launch Bridge LLC")
app.add_middleware(SecurityHeadersMiddleware)

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

db = firestore.Client(project=FIREBASE_PROJECT_ID)
ORDERS = db.collection(ORDERS_COLLECTION)
DOCUMENT_ACCESS_LOG = db.collection("document_access_log")

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

# Canonical order of the order state machine. An order's "state" field is
# always one of these. Progression is mostly linear, but filing_confirmed
# and ein_requested/ein_issued can land out of this textbook order in real
# wall-clock time (SCC and the IRS move at their own pace) - the admin
# dashboard lets the admin set filing_confirmed and ein_issued independently
# of where the automated steps currently are.
ORDER_STATES = [
    "draft", "paid", "name_cleared", "review_approved", "filing_submitted",
    "filing_confirmed", "awaiting_ein_filing", "ein_requested", "ein_issued",
    "assets_generated", "complete",
]
ORDER_STATE_INDEX = {s: i for i, s in enumerate(ORDER_STATES)}

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
    "paid": "Payment received. We're checking your business name's availability with the Virginia State Corporation Commission.",
    "name_cleared": "Your business name is available. Your order is queued for review before we file.",
    "review_approved": "Your order has been approved and we're preparing your filing.",
    "filing_submitted": "Your LLC paperwork has been submitted to the Virginia SCC. SCC approval typically takes several business days - we don't control their timeline and can't promise an exact date. Your EIN application will be filed with the IRS once that approval comes through.",
    # filing_confirmed, ein_requested, and ein_issued have dynamic wording -
    # see compute_state_message() below - since they depend on ein_status/
    # next_available_window or the actual EIN value, not just the state name.
    "assets_generated": "Your brand kit, marketing plan, and business website are ready.",
    "complete": "Everything is ready - here's your full business package.",
}

def compute_state_message(order: dict, state: str) -> str:
    """Most states have a fixed message (STATE_MESSAGES). filing_confirmed,
    ein_requested, and ein_issued need live data - whether the EIN is
    queued on IRS hours, or the actual EIN once issued - so they're built
    here instead."""
    if state in ("filing_confirmed", "awaiting_ein_filing"):
        if order.get("ein_status") == "queued" and order.get("next_available_window"):
            window = datetime.datetime.fromisoformat(order["next_available_window"])
            return (
                f"Your LLC has been approved! Your EIN application will be submitted on "
                f"{window.strftime('%A, %B %d')} at {window.strftime('%I:%M %p').lstrip('0')} Eastern "
                f"when the IRS system opens."
            )
        return "Your Virginia LLC has been officially approved by the SCC. We're now preparing your EIN application."
    if state == "ein_requested":
        return "Your EIN application has been submitted to the IRS. You will receive your EIN shortly."
    if state == "ein_issued":
        return f"Your EIN is: {order.get('ein', '')}. Your business is ready to operate!"
    return STATE_MESSAGES.get(state, "")

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
    """Derives the 10-step customer-facing timeline from the order's raw
    state machine fields. skip_llc_formation/skip_ein customers bypass
    whole steps (filing, SCC approval) rather than just completing them
    instantly, so those get their own "skipped" wording instead of acting
    like the step still happened the normal way."""
    skip_llc = bool(order.get("skip_llc_formation"))
    skip_ein = bool(order.get("skip_ein"))
    business_name = order.get("business_name", "your business")
    email = order.get("email", "")
    steps = []

    # 1. Payment Received
    if state != "draft":
        steps.append({"key": "payment", "name": "Payment Received", "status": "complete",
            "description": f"Payment of $350 confirmed{on_date(order.get('paid_at'))}"})
    else:
        steps.append({"key": "payment", "name": "Payment Received", "status": "pending",
            "description": "We're waiting for your payment to go through."})

    # 2. Name Verification
    name_check = order.get("name_check")
    if skip_llc:
        steps.append({"key": "name", "name": "Name Verification", "status": "complete",
            "description": "Using your existing LLC - already verified on Virginia SCC records"})
    elif name_check and name_check.get("available"):
        steps.append({"key": "name", "name": "Name Verification", "status": "complete",
            "description": f"{business_name} verified as available on Virginia SCC{on_date(order.get('name_cleared_at'))}"})
    elif name_check and not name_check.get("available"):
        steps.append({"key": "name", "name": "Name Verification", "status": "on_hold",
            "description": f"Your filing is on hold - we will contact you at {email} about choosing a different name."})
    else:
        steps.append({"key": "name", "name": "Name Verification",
            "status": "current" if state == "paid" else "pending",
            "description": "Verifying your business name with Virginia SCC..."})

    # 3. Documents Generated
    if order.get("documents_generated"):
        steps.append({"key": "documents", "name": "Documents Generated", "status": "complete",
            "description": f"Articles of Organization, Operating Agreement, and brand kit created{on_date(order.get('documents_generated_at'))}"})
    elif order.get("documents_error"):
        steps.append({"key": "documents", "name": "Documents Generated", "status": "on_hold",
            "description": order["documents_error"]})
    else:
        steps.append({"key": "documents", "name": "Documents Generated",
            "status": "current" if steps[1]["status"] == "complete" else "pending",
            "description": "AI is generating your business documents..."})

    # 4. Internal Review
    if skip_llc:
        steps.append({"key": "review", "name": "Internal Review", "status": "complete",
            "description": "Skipped - no internal review needed for an existing LLC"})
    elif reached(state, "review_approved"):
        steps.append({"key": "review", "name": "Internal Review", "status": "complete",
            "description": f"Your filing has been approved for submission{on_date(order.get('review_approved_at'))}"})
    else:
        steps.append({"key": "review", "name": "Internal Review",
            "status": "current" if state == "name_cleared" else "pending",
            "description": "Our team is reviewing your filing - usually within 2 hours during business hours."})

    # 5. LLC Filed with Virginia SCC
    if skip_llc:
        steps.append({"key": "filed", "name": "LLC Filed with Virginia SCC", "status": "complete",
            "description": "Skipped - using your existing LLC"})
    elif order.get("filing_error"):
        steps.append({"key": "filed", "name": "LLC Filed with Virginia SCC", "status": "on_hold",
            "description": order["filing_error"]})
    elif reached(state, "filing_submitted"):
        steps.append({"key": "filed", "name": "LLC Filed with Virginia SCC", "status": "complete",
            "description": f"Filed with the Virginia SCC{on_date(order.get('filing_submitted_at'))}"})
    elif state == "review_approved":
        steps.append({"key": "filed", "name": "LLC Filed with Virginia SCC", "status": "current",
            "description": "Your LLC is being submitted to the Virginia SCC..."})
    else:
        steps.append({"key": "filed", "name": "LLC Filed with Virginia SCC", "status": "pending",
            "description": "Waiting for internal approval before filing"})

    # 6. LLC Approved by Virginia
    if skip_llc:
        steps.append({"key": "approved", "name": "LLC Approved by Virginia", "status": "complete",
            "description": "Skipped - your existing LLC is already approved"})
    elif reached(state, "filing_confirmed"):
        steps.append({"key": "approved", "name": "LLC Approved by Virginia", "status": "complete",
            "description": f"Virginia approved {business_name}{on_date(order.get('filing_confirmed_at'))}"})
    else:
        steps.append({"key": "approved", "name": "LLC Approved by Virginia",
            "status": "current" if state == "filing_submitted" else "pending",
            "description": "Waiting for Virginia SCC to process your filing (1-3 business days)"})

    # 7. EIN Application
    ein = order.get("ein")
    ein_status = order.get("ein_status")
    if ein and reached(state, "ein_issued"):
        if ein_status == "provided_by_customer":
            desc = f"EIN {ein} - provided by you"
        else:
            desc = f"EIN {ein} issued by IRS{on_date(order.get('ein_issued_at'))}"
        steps.append({"key": "ein", "name": "EIN Application", "status": "complete", "description": desc})
    elif order.get("ein_error"):
        steps.append({"key": "ein", "name": "EIN Application", "status": "on_hold", "description": order["ein_error"]})
    elif ein_status == "queued" and order.get("next_available_window"):
        window = order["next_available_window"]
        if isinstance(window, str):
            window = datetime.datetime.fromisoformat(window)
        steps.append({"key": "ein", "name": "EIN Application", "status": "current",
            "description": f"EIN application queued - will be submitted {format_eta(window)}"})
    elif state == "ein_requested":
        steps.append({"key": "ein", "name": "EIN Application", "status": "current",
            "description": "EIN application submitted to IRS"})
    elif state == "awaiting_ein_filing":
        steps.append({"key": "ein", "name": "EIN Application", "status": "current",
            "description": "Your SSN is on file - filing your EIN with the IRS shortly"})
    elif reached(state, "filing_confirmed"):
        steps.append({"key": "ein", "name": "EIN Application", "status": "current",
            "description": "Preparing your EIN application..."})
    else:
        steps.append({"key": "ein", "name": "EIN Application", "status": "pending",
            "description": "Will be submitted after LLC approval"})

    # 8. Business Website Live
    website_url = order.get("website_url")
    if website_url:
        steps.append({"key": "website", "name": "Business Website Live", "status": "complete",
            "description": f"Your website is live at {website_url}", "url": website_url})
    elif order.get("asset_generation_error"):
        steps.append({"key": "website", "name": "Business Website Live", "status": "on_hold",
            "description": order["asset_generation_error"]})
    elif reached(state, "ein_issued"):
        steps.append({"key": "website", "name": "Business Website Live", "status": "current",
            "description": "Generating your business website..."})
    else:
        steps.append({"key": "website", "name": "Business Website Live", "status": "pending",
            "description": "Website will be generated after LLC approval"})

    # 9. Stripe Payment Account
    connect_id = order.get("stripe_connect_account_id")
    if connect_id:
        if is_account_active(connect_id):
            steps.append({"key": "stripe", "name": "Stripe Payment Account", "status": "complete",
                "description": "✅ Your payment account is active - you can accept payments"})
        else:
            steps.append({"key": "stripe", "name": "Stripe Payment Account", "status": "current",
                "description": "Your Stripe account is ready - finish setup to start accepting payments",
                "onboarding": True})
    elif reached(state, "ein_issued"):
        steps.append({"key": "stripe", "name": "Stripe Payment Account", "status": "current",
            "description": "Setting up your Stripe payment account..."})
    else:
        steps.append({"key": "stripe", "name": "Stripe Payment Account", "status": "pending",
            "description": "Stripe account will be created after LLC approval"})

    # 10. Complete
    if state == "complete":
        steps.append({"key": "complete", "name": "Complete", "status": "complete",
            "description": "🎉 Your business is fully operational!"})
    else:
        steps.append({"key": "complete", "name": "Complete", "status": "pending",
            "description": "Almost there..."})

    return steps

def estimate_completion(order: dict, state: str) -> str:
    """Rough estimate, not a promise - SCC and the IRS move at their own
    pace and we say so elsewhere on the page. Just gives the customer a
    ballpark instead of nothing."""
    if state == "complete":
        return None

    today = datetime.date.today()
    if order.get("skip_llc_formation"):
        days_out = 0 if reached(state, "ein_issued") else 1
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

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "cancelled": request.query_params.get("cancelled") == "1"
    })

@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html")

@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse(request, "terms.html")

@app.post("/screen-name", response_class=HTMLResponse)
async def screen_name(request: Request):
    form = await request.form()
    result = screen_business_name(form.get("business_idea", ""))
    return templates.TemplateResponse(request, "result.html", {"result": result})

@app.post("/check-name", response_class=HTMLResponse)
async def check_name(request: Request):
    form = await request.form()
    desired_name = form.get("desired_name", "")
    loop = asyncio.get_event_loop()

    scc_result, gemini_result = await asyncio.gather(
        loop.run_in_executor(None, check_name_on_scc, desired_name),
        loop.run_in_executor(None, check_business_name, desired_name, "Virginia"),
        return_exceptions=True,
    )
    # Each check must stand on its own - a Gemini hiccup shouldn't blank out
    # a working SCC result, and vice versa.
    if isinstance(scc_result, Exception):
        scc_result = {"available": None, "status": "ERROR", "message": str(scc_result), "conflicts": [], "raw": ""}
    if isinstance(gemini_result, Exception):
        gemini_result = {
            "status": "error", "domain": "", "domain_available": None,
            "gemini_analysis": "Trademark analysis is temporarily unavailable - please try again.",
            "scc_url": SCC_NAME_CHECK_URL,
        }

    return templates.TemplateResponse(request, "name_check_result.html", {
        "result": gemini_result,
        "scc": scc_result,
        "desired_name": desired_name
    })

@app.post("/verify-existing-llc", response_class=HTMLResponse)
async def verify_existing_llc(request: Request):
    """Live-checked while the customer types their existing LLC's name on
    the intake form (skip_llc_formation path) - confirms it's actually on
    Virginia SCC's books before we let them skip formation for it."""
    form = await request.form()
    existing_llc_name = form.get("existing_llc_name", "").strip()
    if not existing_llc_name:
        return HTMLResponse("")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, check_llc_exists_on_scc, existing_llc_name)

    return templates.TemplateResponse(request, "existing_llc_verify_result.html", {
        "result": result,
        "business_name": existing_llc_name,
    })

def parse_intake_form(form: dict) -> dict:
    """Pulls every derived field the agents need out of the raw intake
    form dict. Used both right after submission (to size the Checkout
    description) and again whenever a background step re-reads the same
    fields back out of the Firestore order document."""
    first_name = form.get("first_name", "")
    middle_name = form.get("middle_name", "")
    last_name = form.get("last_name", "")
    sig_first = form.get("sig_first", "")
    sig_middle = form.get("sig_middle", "")
    sig_last = form.get("sig_last", "")
    address = form.get("address", "")
    city = form.get("city", "")
    zipcode = form.get("zipcode", "")
    desired_name = form.get("desired_name", "")
    existing_llc_name = form.get("existing_llc_name", "")
    skip_llc_formation = form.get("skip_llc_formation") == "on"

    full_name = f"{first_name} {middle_name} {last_name}".replace("  ", " ").strip()
    primary_sig = f"{sig_first} {sig_middle} {sig_last}".replace("  ", " ").strip()
    principal_address = f"{address}, {city}, VA {zipcode}"

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
    if skip_llc_formation:
        business_name = existing_llc_name.strip()
    else:
        business_name = desired_name.strip() if desired_name.strip() else f"{last_name} Ventures LLC"

    return {
        "full_name": full_name,
        "primary_sig": primary_sig,
        "principal_address": principal_address,
        "all_signatures": all_signatures,
        "business_name": business_name,
    }

def run_name_check(order_id: str):
    """Runs automatically right after payment. Advances paid -> name_cleared
    if the business name is available on Virginia SCC; otherwise leaves the
    order at "paid" with the check result stored for the admin to see and
    act on (the automated pipeline doesn't know how to fix a taken name).

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
        if result.get("available") or result.get("status") == "UNAVAILABLE":
            record_state(order_ref, "name_cleared", name_cleared_at=firestore.SERVER_TIMESTAMP)
            run_document_generation(order_id)
        else:
            print(f"⚠️ Name check did not clear for order {order_id}: {result.get('message')}")
    except Exception as e:
        print(f"⚠️ Name check crashed for order {order_id}: {e}")
        order_ref.set({"name_check_error": str(e)}, merge=True)

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

    business_name = order.get("business_name", "")
    full_name = order.get("full_name", "")
    business_purpose = order.get("business_purpose", "")
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
            brand_result = generate_brand_kit(business_name, business_idea, target_customer)
            update["brand_result"] = brand_result
            object_name = upload_document(order_id, brand_result.get("result", "").encode("utf-8"), "text/plain", "txt")
            update["documents.brand_kit"] = {"object_name": object_name, "uploaded_at": firestore.SERVER_TIMESTAMP}
        except Exception as e:
            print(f"⚠️ Brand kit agent failed for order {order_id}: {e}")
            errors["brand_result"] = f"Brand kit: {e}"

    if not order.get("marketing_result"):
        try:
            update["marketing_result"] = generate_marketing_plan(business_name, business_idea, "Virginia", target_customer)
        except Exception as e:
            print(f"⚠️ Marketing plan agent failed for order {order_id}: {e}")
            errors["marketing_result"] = f"Marketing plan: {e}"

    if not existing_documents.get("articles"):
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
        update.get("marketing_result") or order.get("marketing_result"),
        update.get("documents.articles") or existing_documents.get("articles"),
    ])
    newly_generated = have_all and not order.get("documents_generated")
    update["documents_generated"] = have_all
    if have_all:
        update["documents_generated_at"] = firestore.SERVER_TIMESTAMP
    update["documents_error"] = "; ".join(errors.values()) if errors else firestore.DELETE_FIELD
    # .update(), not .set(merge=True) - the dotted "documents.articles"-style
    # keys above are only treated as nested field paths (rather than literal
    # field names containing dots) by .update().
    order_ref.update(update)

    if newly_generated:
        send_documents_ready_email(order, order_id)

SCC_FILED_STATES = {
    "filing_submitted", "filing_confirmed", "awaiting_ein_filing",
    "ein_requested", "ein_issued", "assets_generated", "complete",
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
    Two independent detectors can both notice the same real-world SCC
    approval - the hourly name-search poller (app/check_scc_status.py)
    and the 5-minute Gmail poller (app/gmail_poller.py) - and either one
    might win the race to call this first. Without this guard the loser
    would re-send the approved/EIN-issued email and re-run asset
    generation a second time."""
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
            "registered_agent_choice": order.get("registered_agent_choice", "launchbridge"),
        }
        llc_filed = file_llc_on_scc(llc_customer_data, interactive=False)
        if not llc_filed:
            order_ref.set({"filing_error": "SCC filing did not complete - check server screenshots, then re-approve to retry."}, merge=True)
            return

        record_state(order_ref, "filing_submitted",
            filing_submitted_at=firestore.SERVER_TIMESTAMP,
            filing_error=firestore.DELETE_FIELD,
        )
        send_llc_filed_email(order, order_id)
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
        result = file_ein_with_irs(ein_customer_data, interactive=False, on_submitted=on_submitted)

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

def run_asset_generation(order_id: str):
    """Triggered once the admin records the real EIN (or immediately for
    skip_ein orders). Documents (brand kit, marketing plan, signed LLC PDF)
    are already done by now - see run_document_generation, which runs much
    earlier, right after name verification. This step just sets up the
    customer's Stripe Connect Standard account and a pay-what-you-want
    Payment Link on it, then deploys the business website with that link
    already embedded."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return

    try:
        business_name = order["business_name"]
        business_idea = order["business_idea"]
        target_customer = order["target_customer"]
        principal_address = order["principal_address"]
        email = order["email"]
        phone = order["phone"]

        # Re-entrant by field, not by a single all-or-nothing flag - a
        # retry (see /admin/{order_id}/retry-agents) only redoes whichever
        # of Stripe/website didn't already succeed, instead of recreating
        # a second Stripe Connect account or re-deploying a working site.
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

        website_url = order.get("website_url")
        if not website_url:
            try:
                services = [
                    {"name": order.get(f"service_{i}_name", ""), "description": order.get(f"service_{i}_desc", "")}
                    for i in (1, 2, 3)
                ]
                photos = [order.get(f"photo_{i}_data") for i in (1, 2, 3)]
                result = generate_website(
                    business_name, business_idea, target_customer, email, phone, principal_address,
                    template_name=order.get("website_template", "professional"),
                    tagline=order.get("website_tagline", ""),
                    description=order.get("website_description", ""),
                    services=services,
                    hours=order.get("business_hours", ""),
                    photos=photos,
                    instagram_url=order.get("instagram_url", ""),
                    facebook_url=order.get("facebook_url", ""),
                    tiktok_url=order.get("tiktok_url", ""),
                    color_preference=order.get("color_preference", "default"),
                    custom_primary_color=order.get("custom_primary_color", ""),
                    payment_link_url=payment_link_url,
                )
                deployed = deploy_website(business_name, result["html"], order_id=order_id)
                if deployed:
                    website_url = deployed["url"]
                    order_ref.set({"website_template": result["template"], "website_content": result["content"]}, merge=True)
                else:
                    # deploy_website returns None (not a raise) on a failed
                    # GitHub push/Pages call - this was previously silent, with
                    # nothing recorded anywhere and the order advancing to
                    # "complete" regardless. See the website-deploy log output
                    # for the actual GitHub API error.
                    print(f"⚠️ Website deploy returned no URL for order {order_id} - check logs above for the GitHub API error.")
                    asset_error = ((asset_error + " ") if asset_error else "") + "Could not deploy your business website - check server logs, then retry."
            except Exception as e:
                print(f"⚠️ Website generation/deploy failed for order {order_id}: {e}")
                asset_error = ((asset_error + " ") if asset_error else "") + f"Website generation crashed unexpectedly: {e}"

        record_state(order_ref, "assets_generated",
            website_url=website_url,
            stripe_connect_account_id=connect_account_id,
            stripe_payment_link_url=payment_link_url,
            assets_generated_at=firestore.SERVER_TIMESTAMP,
            asset_generation_error=asset_error if asset_error else firestore.DELETE_FIELD,
        )

        # Only the live website actually proves this order is done - a
        # Stripe or website failure must never be masked by a state that
        # tells the customer everything is ready.
        if website_url:
            record_state(order_ref, "complete")
            order["website_url"] = website_url
            order["stripe_connect_account_id"] = connect_account_id
            send_website_live_email(order, order_id)
            send_admin_sms(f"🌐 Site live! {business_name}")
            send_everything_complete_email(order, order_id)
            send_admin_sms(f"🎉 Done! {business_name} fully onboarded")
    except Exception as e:
        print(f"⚠️ Asset generation crashed for order {order_id}: {e}")
        order_ref.set({"asset_generation_error": f"Asset generation crashed unexpectedly: {e}. Check server logs."}, merge=True)

@app.post("/launch", response_class=HTMLResponse)
async def launch(request: Request):
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
                except Exception as e:
                    photo_errors[f"photo_{i}"] = f"Could not process photo {i} - try a different file."

    form = {k: v for k, v in form_raw.items() if not (k.startswith("photo_") and hasattr(v, "filename"))}
    form.setdefault("registered_agent_choice", "launchbridge")

    errors = validate_intake_form(form)
    errors.update(photo_errors)
    if errors:
        return templates.TemplateResponse(request, "form_errors.html", {
            "errors": errors,
            "all_fields": ALL_VALIDATED_FIELDS,
        })

    parsed = parse_intake_form(form)

    order_ref = ORDERS.document()
    order_id = order_ref.id
    # Written immediately, before the Stripe call below - so the customer's
    # data is never lost even if checkout-session creation fails or their
    # browser closes before the redirect, only the (re-creatable) Stripe
    # session would be missing, not their submitted information.
    record_state(order_ref, "draft", **form, **parsed, **photo_data, created_at=firestore.SERVER_TIMESTAMP)

    base_url = str(request.base_url)
    try:
        session = create_checkout_session(
            order_id=order_id,
            business_name=parsed["business_name"],
            success_url=f"{base_url}success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}cancel",
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
            "all_fields": ALL_VALIDATED_FIELDS,
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

def process_paid_order(order_id: str, payment_status: str, background_tasks: BackgroundTasks) -> bool:
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
    send_order_received_email(order, order_id)
    send_admin_sms(f"🚀 New order! {order.get('business_name', '')} - {order.get('full_name', '')} paid $350")

    if needs_ssn(order):
        record_state(order_ref, "paid", awaiting_ssn=True)
        return True

    if order.get("skip_llc_formation"):
        # Already verified to exist on SCC at intake time (see
        # /verify-existing-llc) - nothing left to file or check the
        # name of, so jump straight past filing_confirmed.
        # advance_past_filing_confirmed (below) does the actual state
        # transition (to filing_confirmed, or straight to ein_issued for
        # skip_ein too) - run_document_generation doesn't read order.state
        # so it's safe to fire before that lands.
        background_tasks.add_task(run_document_generation, order_id)
        trigger_assets = advance_past_filing_confirmed(order_ref, order)
        if trigger_assets:
            background_tasks.add_task(run_asset_generation, order_id)
    else:
        record_state(order_ref, "paid")
        background_tasks.add_task(run_name_check, order_id)

    return True

def start_pipeline_after_ssn(order_id: str, background_tasks: BackgroundTasks):
    """Picks up exactly where process_paid_order left off for an order
    that was waiting on an SSN - clears awaiting_ssn and kicks off the
    same pipeline process_paid_order would have started immediately had
    the SSN not been needed. ssn_collected_at is set by encrypt_ssn
    itself, not here."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return

    order_ref.set({"awaiting_ssn": False}, merge=True)

    if order.get("skip_llc_formation"):
        background_tasks.add_task(run_document_generation, order_id)
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
async def success(request: Request, background_tasks: BackgroundTasks, session_id: str = None, order_id: str = None):
    resolved_order_id = order_id

    if session_id:
        try:
            session = retrieve_checkout_session(session_id)
        except Exception as e:
            return HTMLResponse(f"<p>Could not verify payment: {e}</p>", status_code=400)

        resolved_order_id = session.client_reference_id
        if not resolved_order_id:
            return RedirectResponse(url="/")

        process_paid_order(resolved_order_id, session.payment_status, background_tasks)

    if not resolved_order_id:
        return RedirectResponse(url="/")

    order = ORDERS.document(resolved_order_id).get().to_dict()
    if not order:
        return RedirectResponse(url="/")

    # Completing Stripe Checkout proves payment, not email ownership - so
    # this must not mint a dashboard session directly. The customer's
    # first magic link arrives via the order-received email instead (see
    # send_order_received_email), sent from inside process_paid_order
    # above.
    return templates.TemplateResponse(request, "success_interstitial.html", {
        "email": order.get("email", ""),
    })

# Locked down hard, on this one route only - the SSN page must never be
# able to load a third-party script, image, or outbound connection, so a
# compromised CDN or injected tag can't exfiltrate anything typed here.
SSN_PAGE_CSP = "default-src 'self'; script-src 'none'; img-src 'none'; connect-src 'none'; style-src 'self' 'unsafe-inline'"

@app.get("/collect-ssn/{order_id}", response_class=HTMLResponse)
async def collect_ssn_page(request: Request, order_id: str):
    order = ORDERS.document(order_id).get().to_dict()
    if not order:
        return HTMLResponse("<p>Order not found.</p>", status_code=404)
    expired = bool(order.get("ssn_expired")) or needs_ssn_reentry(order, order_id)
    if not order.get("awaiting_ssn") and not expired:
        return RedirectResponse(url=f"/status/{order_id}")

    return templates.TemplateResponse(request, "collect_ssn.html", {
        "order_id": order_id,
        "business_name": order.get("business_name", ""),
        "expired": expired,
        "error": None,
    }, headers={"Content-Security-Policy": SSN_PAGE_CSP})

@app.post("/collect-ssn/{order_id}", response_class=HTMLResponse)
async def collect_ssn_submit(request: Request, order_id: str, background_tasks: BackgroundTasks):
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return HTMLResponse("<p>Order not found.</p>", status_code=404)
    expired = bool(order.get("ssn_expired")) or needs_ssn_reentry(order, order_id)
    if not order.get("awaiting_ssn") and not expired:
        return RedirectResponse(url=f"/status/{order_id}")

    form = await request.form()
    ssn = (form.get("ssn") or "").strip()
    error = validate_ssn(ssn)
    if error:
        return templates.TemplateResponse(request, "collect_ssn.html", {
            "order_id": order_id,
            "business_name": order.get("business_name", ""),
            "expired": expired,
            "error": error,
        }, status_code=400, headers={"Content-Security-Policy": SSN_PAGE_CSP})

    if not encrypt_ssn(ssn, order_id):
        return templates.TemplateResponse(request, "collect_ssn.html", {
            "order_id": order_id,
            "business_name": order.get("business_name", ""),
            "expired": expired,
            "error": "Could not securely store your SSN - please try again, or contact support@launchbridge.ai if this keeps happening.",
        }, status_code=500, headers={"Content-Security-Policy": SSN_PAGE_CSP})

    if expired:
        # The LLC is already SCC-approved by the time an SSN expires and
        # gets re-entered - this is the only path where the EIN-ready
        # admin SMS/email (see notify_ein_ready) needs to fire right here,
        # since filing_confirmed already happened long ago.
        resume_ein_after_ssn_reentry(order_id)
    else:
        # Normal flow: SSN is collected right after payment, before the
        # LLC has even been name-checked yet - nothing EIN-related is
        # ready to notify about. notify_ein_ready fires later on its own,
        # from advance_past_filing_confirmed, once SCC approval comes in
        # and finds the SSN already sitting in the vault.
        start_pipeline_after_ssn(order_id, background_tasks)
    return RedirectResponse(url=f"/status/{order_id}", status_code=303)

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
        })
        return Response(status_code=400)

    log_entry = {
        "verified": True, "event_id": event.id, "event_type": event.type,
        "received_at": firestore.SERVER_TIMESTAMP,
    }

    if event.type == "checkout.session.completed":
        session = event.data.object
        order_id = session.client_reference_id
        log_entry["order_id"] = order_id
        if order_id:
            advanced = process_paid_order(order_id, session.payment_status, background_tasks)
            log_entry["advanced_order"] = advanced

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
        "estimate_breakdown": "LLC approval: 1-3 business days · EIN: same day after approval · Website: automatic",
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

STATUS_TOKEN_TTL_SECONDS = 86400  # 24 hours

def _status_cookie_name(order_id: str) -> str:
    return f"sv_{order_id}"

def _make_status_token(order_id: str) -> str:
    """A stateless, signed "this browser verified the email for this
    order" claim - no server-side session storage needed. Scoped to one
    order_id (baked into the signed payload, not just the cookie name) so
    a token can't be replayed against a different order even if the
    cookie name were somehow reused."""
    expiry = int(datetime.datetime.now(datetime.timezone.utc).timestamp()) + STATUS_TOKEN_TTL_SECONDS
    payload = f"{order_id}:{expiry}"
    sig = hmac.new(STATUS_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}:{sig}"

def _verify_status_token(order_id: str, token: str) -> bool:
    if not token or ":" not in token:
        return False
    expiry_str, _, sig = token.partition(":")
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if expiry < datetime.datetime.now(datetime.timezone.utc).timestamp():
        return False
    expected = hmac.new(STATUS_SESSION_SECRET.encode(), f"{order_id}:{expiry}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)

EIN_FILING_LINK_TTL_SECONDS = 86400  # 24 hours - regenerated fresh each time notify_ein_ready fires

def _make_ein_filing_link_token(order_id: str) -> str:
    """Same scheme as _make_status_token, but with an "ein-filing:" prefix
    baked into the signed payload - sharing STATUS_SESSION_SECRET across
    the two token types is fine precisely because each payload says what
    kind of claim it is, so one can never be replayed as the other even
    though both verify against the same secret."""
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

STATUS_IP_RATE_LIMIT = 10
STATUS_IP_RATE_WINDOW = datetime.timedelta(minutes=1)
STATUS_EMAIL_FAIL_LIMIT = 3
STATUS_EMAIL_FAIL_WINDOW = datetime.timedelta(hours=1)

ORDER_ID_REQUEST_LIMIT = 3
ORDER_ID_REQUEST_WINDOW = datetime.timedelta(hours=1)

@app.post("/request-order-id", response_class=HTMLResponse)
async def request_order_id(request: Request):
    """Part 2 of the order-tracking system: a customer who's lost their
    order ID can look it up by the email they originally used. Rate
    limited per-email via a Firestore log (not an in-memory counter) so
    the limit holds even with multiple Cloud Run instances."""
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    if not email:
        return HTMLResponse('<p class="text-red-400 text-sm mt-2">Please enter your email address.</p>', status_code=400)

    if _rate_limited("order_id_requests", "email", email, ORDER_ID_REQUEST_WINDOW, ORDER_ID_REQUEST_LIMIT):
        return HTMLResponse(
            '<p class="text-yellow-400 text-sm mt-2">Too many requests for this email — please try again in an '
            'hour, or contact <a href="mailto:support@launchbridge.ai" class="underline">support@launchbridge.ai</a>.</p>',
            status_code=429,
        )

    matches = list(ORDERS.where("email", "==", email).stream())
    if not matches:
        return HTMLResponse(
            '<p class="text-red-400 text-sm mt-2">No order found for this email. Contact '
            '<a href="mailto:support@launchbridge.ai" class="underline">support@launchbridge.ai</a>.</p>',
            status_code=404,
        )

    # If the same email placed more than one order, the most recently
    # created one is almost always the one they're asking about.
    latest = max(matches, key=lambda d: d.to_dict().get("created_at") or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))
    send_order_id_email(latest.to_dict(), latest.id)

    return HTMLResponse(f'<p class="text-green-400 text-sm mt-2">Check your email! We sent your order ID to {email}.</p>')

def _status_gate_context(order_id: str, order: dict, error: str = None) -> dict:
    return {
        "order_id": order_id,
        "business_name": order.get("business_name"),
        "state_label": order.get("state", "draft").replace("_", " ").title(),
        "error": error,
    }

@app.get("/status/{order_id}", response_class=HTMLResponse)
async def status_page(request: Request, order_id: str):
    order_snap = ORDERS.document(order_id).get()
    if not order_snap.exists:
        return templates.TemplateResponse(request, "order_not_found.html", {}, status_code=404)
    order = order_snap.to_dict()
    order = ensure_payment_link(order_snap.reference, order)
    state = order.get("state", "draft")

    if order.get("awaiting_ssn"):
        return RedirectResponse(url=f"/collect-ssn/{order_id}")

    if _rate_limited("status_ip_log", "ip", get_client_ip(request), STATUS_IP_RATE_WINDOW, STATUS_IP_RATE_LIMIT):
        return HTMLResponse("<p>Too many requests - please wait a minute and try again.</p>", status_code=429)

    if not _verify_status_token(order_id, request.cookies.get(_status_cookie_name(order_id), "")):
        # Unverified visitors get only the business name and current
        # state - everything else (EIN, timeline detail, documents,
        # contact form prefilled with their email, etc) waits behind the
        # email check below.
        return templates.TemplateResponse(request, "status_gate.html", _status_gate_context(order_id, order))

    return templates.TemplateResponse(request, "status.html", {
        **status_context(order_id, order),
        "business_name": order.get("business_name"),
        "full_name": order.get("full_name"),
        "email": order.get("email"),
        "registered_agent_choice": order.get("registered_agent_choice", "launchbridge"),
        "ein": order.get("ein"),
        "has_ein_letter": bool(order.get("ein_letter_uploaded_at")),
        "website_url": order.get("website_url"),
        "onboarding_url": f"/connect/onboard/{order_id}" if order.get("stripe_connect_account_id") else None,
        "needs_ssn_reentry": needs_ssn_reentry(order, order_id),
    })

@app.post("/status/{order_id}/verify", response_class=HTMLResponse)
async def status_verify(request: Request, order_id: str):
    """The email gate's submit target. On a match, sets a signed,
    httponly/secure/samesite=strict cookie (see _make_status_token) good
    for 24 hours and scoped to this order's own status routes, then
    redirects back to GET /status/{order_id} to render the full page."""
    order_snap = ORDERS.document(order_id).get()
    if not order_snap.exists:
        return templates.TemplateResponse(request, "order_not_found.html", {}, status_code=404)
    order = order_snap.to_dict()

    if _rate_limited("status_ip_log", "ip", get_client_ip(request), STATUS_IP_RATE_WINDOW, STATUS_IP_RATE_LIMIT):
        return HTMLResponse("<p>Too many requests - please wait a minute and try again.</p>", status_code=429)

    form = await request.form()
    submitted_email = (form.get("email") or "").strip().lower()
    order_email = (order.get("email") or "").strip().lower()

    if not submitted_email or submitted_email != order_email:
        if _rate_limited("status_verify_failures", "order_id", order_id, STATUS_EMAIL_FAIL_WINDOW, STATUS_EMAIL_FAIL_LIMIT):
            return templates.TemplateResponse(request, "status_gate.html", _status_gate_context(
                order_id, order, "Too many failed attempts - please try again in an hour, or contact support@launchbridge.ai."
            ), status_code=429)
        return templates.TemplateResponse(request, "status_gate.html", _status_gate_context(
            order_id, order, "Email not found for this order."
        ), status_code=403)

    response = RedirectResponse(url=f"/status/{order_id}", status_code=303)
    response.set_cookie(
        key=_status_cookie_name(order_id), value=_make_status_token(order_id),
        max_age=STATUS_TOKEN_TTL_SECONDS, httponly=True, secure=True, samesite="strict",
        path=f"/status/{order_id}",
    )
    return response

@app.get("/status/{order_id}/timeline", response_class=HTMLResponse)
async def status_timeline_partial(request: Request, order_id: str):
    """Polled every 30s by the status page (see status.html) to refresh
    just the timeline + estimate, without reloading documents/contact/share
    sections that don't change nearly as often. Gated by the same
    verification cookie as the full page - without this check it would be
    an unauthenticated backdoor to the same EIN/timeline detail the gate
    is supposed to be hiding."""
    order_snap = ORDERS.document(order_id).get()
    if not order_snap.exists:
        return HTMLResponse("")
    order = order_snap.to_dict()
    order = ensure_payment_link(order_snap.reference, order)

    if not _verify_status_token(order_id, request.cookies.get(_status_cookie_name(order_id), "")):
        return HTMLResponse("")

    return templates.TemplateResponse(request, "status_timeline.html", {
        **status_context(order_id, order),
        "onboarding_url": f"/connect/onboard/{order_id}" if order.get("stripe_connect_account_id") else None,
    })

# ─── Customer dashboard (magic-link auth) ──────────────────────────────
# Replaces /status/* + /collect-ssn/* + the four /download-* routes once
# fully verified on staging (see the security-rework plan) - built
# alongside the routes above rather than in place of them, so the old
# flow keeps working in production until everything here is promoted.

DASHBOARD_SESSION_COOKIE = "lb_session"
MAGIC_LINK_RATE_WINDOW = datetime.timedelta(hours=1)
MAGIC_LINK_RATE_LIMIT = 3

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
async def dashboard_verify(request: Request, token: str = "", exp: str = "", sig: str = ""):
    email = redeem_magic_link(token, exp, sig)
    if not email:
        return templates.TemplateResponse(request, "dashboard_login.html", {
            "error": "This link is invalid or has expired - request a new one below.",
        }, status_code=400)

    session_id = create_session(email)
    response = RedirectResponse(url="/dashboard/orders", status_code=303)
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
        return RedirectResponse(url=f"/dashboard/orders/{orders[0].id}")

    return templates.TemplateResponse(request, "dashboard_order_list.html", {
        "orders": [{"order_id": doc.id, "business_name": doc.to_dict().get("business_name")} for doc in orders],
    })

def _dashboard_order_context(request: Request, order_ref, order: dict, ssn_error: str = None) -> dict:
    """Shared between the order page and the SSN-submit route (which
    re-renders the same page with an error instead of redirecting, so the
    customer doesn't lose their place) - keeps both in sync rather than
    risking the two contexts drifting apart."""
    order_id = order_ref.id
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    ssn_expired = bool(order.get("ssn_expired")) or needs_ssn_reentry(order, order_id)
    return {
        **status_context(order_id, order),
        "business_name": order.get("business_name"),
        "full_name": order.get("full_name"),
        "email": order.get("email"),
        "registered_agent_choice": order.get("registered_agent_choice", "launchbridge"),
        "ein": order.get("ein"),
        "website_url": order.get("website_url"),
        "onboarding_url": f"/connect/onboard/{order_id}" if order.get("stripe_connect_account_id") else None,
        "needs_ssn_entry": bool(order.get("awaiting_ssn")) or ssn_expired,
        "ssn_expired": ssn_expired,
        "ssn_error": ssn_error,
        "csrf_token": make_csrf_token(session_id),
        "document_labels": DOCUMENT_LABELS,
        "available_documents": {
            doc_id: _document_object_name(order, order_id, doc_id) is not None
            for doc_id in DOCUMENT_LABELS
        },
    }

@app.get("/dashboard/orders/{order_id}", response_class=HTMLResponse)
async def dashboard_order(request: Request, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order = ensure_payment_link(order_ref, order)
    return templates.TemplateResponse(request, "dashboard_order.html", _dashboard_order_context(request, order_ref, order))

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

    if expired:
        resume_ein_after_ssn_reentry(order_id)
    else:
        start_pipeline_after_ssn(order_id, background_tasks)

    return RedirectResponse(url=f"/dashboard/orders/{order_id}", status_code=303)

@app.get("/dashboard/orders/{order_id}/timeline", response_class=HTMLResponse)
async def dashboard_order_timeline(request: Request, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id
    order = ensure_payment_link(order_ref, order)
    return templates.TemplateResponse(request, "status_timeline.html", {
        **status_context(order_id, order),
        "onboarding_url": f"/connect/onboard/{order_id}" if order.get("stripe_connect_account_id") else None,
    })

DOCUMENT_LABELS = {
    "certificate": "Certificate of Organization",
    "ein_letter": "EIN Confirmation Letter (CP575)",
    "articles": "Articles of Organization",
    "operating_agreement": "Operating Agreement",
    "brand_kit": "Brand Kit",
}

# Orders uploaded before document_store.py existed have no "documents"
# map at all - these are the deterministic legacy paths storage_service.py
# always used, kept readable here (signed URLs work on any object
# regardless of who uploaded it) rather than re-uploading old files.
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

@app.get("/orders/{order_id}/documents/{doc_id}")
async def get_order_document(request: Request, doc_id: str, owned: tuple = Depends(get_owned_order)):
    order_ref, order, customer_id = owned
    order_id = order_ref.id

    if doc_id not in DOCUMENT_LABELS:
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
async def connect_onboard(order_id: str):
    order = ORDERS.document(order_id).get().to_dict()
    if not order or not order.get("stripe_connect_account_id"):
        return RedirectResponse(url="/")

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
        return_url=f"{base_url}/status/{order_id}",
    )
    return RedirectResponse(url=url)

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, authorized: bool = Depends(verify_admin)):
    orders = []
    query = ORDERS.order_by("created_at", direction=firestore.Query.DESCENDING)
    for doc in query.stream():
        order = doc.to_dict()
        order["id"] = doc.id
        orders.append(order)

    irs_open = is_irs_open()
    irs_next_window_eta = None if irs_open else format_eta(next_irs_open())

    for order in orders:
        if order.get("next_available_window"):
            order["next_available_window_eta"] = format_eta(datetime.datetime.fromisoformat(order["next_available_window"]))

    def fmt_datetime(ts) -> str:
        return ts.strftime("%B %-d, %Y %I:%M %p").replace(" 0", " ") if ts else None

    gmail_poller_status = db.collection("system").document("gmail_poller").get().to_dict() or {}
    gmail_poller_status["last_checked_display"] = fmt_datetime(gmail_poller_status.get("last_checked_at"))

    processed_scc_emails = []
    email_log_query = db.collection("processed_scc_emails").order_by("processed_at", direction=firestore.Query.DESCENDING).limit(15)
    for doc in email_log_query.stream():
        entry = doc.to_dict()
        entry["id"] = doc.id
        entry["processed_display"] = fmt_datetime(entry.get("processed_at"))
        processed_scc_emails.append(entry)

    return templates.TemplateResponse(request, "admin.html", {
        "orders": orders,
        "irs_open": irs_open,
        "irs_next_window_eta": irs_next_window_eta,
        "warning": request.query_params.get("warning"),
        "gmail_poller_status": gmail_poller_status,
        "processed_scc_emails": processed_scc_emails,
    })

@app.post("/admin/{order_id}/approve")
async def admin_approve(order_id: str, background_tasks: BackgroundTasks, authorized: bool = Depends(verify_admin)):
    record_state(ORDERS.document(order_id), "review_approved", review_approved_at=firestore.SERVER_TIMESTAMP)
    background_tasks.add_task(run_scc_filing, order_id)
    return RedirectResponse(url="/admin", status_code=303)

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
        mark_ein_issued(order_ref, order, order_id, ein, background_tasks)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/{order_id}/retry-agents")
async def admin_retry_agents(order_id: str, background_tasks: BackgroundTasks, authorized: bool = Depends(verify_admin)):
    """Re-runs whichever agent steps haven't actually succeeded yet for an
    existing, already-paid order - never touches Stripe/payment, so the
    customer is never charged again. Safe to call regardless of how far
    the order has gotten: run_document_generation and run_asset_generation
    both skip any piece (name/brand/marketing/PDF, Stripe Connect,
    website) that already has a result on file and only retry what's
    missing or previously errored."""
    order_ref = ORDERS.document(order_id)
    if not order_ref.get().exists:
        return RedirectResponse(url="/admin", status_code=303)

    background_tasks.add_task(run_document_generation, order_id)
    background_tasks.add_task(run_asset_generation, order_id)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/contact", response_class=HTMLResponse)
async def contact(request: Request):
    form = await request.form()
    order_id = (form.get("order_id") or "").strip()
    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip()
    message = (form.get("message") or "").strip()

    # Only an authenticated dashboard session needs CSRF protection - the
    # legacy, unauthenticated status page has no session to protect and
    # has no csrf_token field at all, so it's left alone. This is the one
    # route in the rework where a real 403 is correct, since a CSRF
    # mismatch is "stale form," not an ownership question.
    session_id = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    if session_id and verify_and_touch_session(session_id):
        if not verify_csrf_token(session_id, (form.get("csrf_token") or "").strip()):
            raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    if not (name and email and message):
        return HTMLResponse('<p class="text-red-400 text-sm mt-2">Please fill in your name, email, and message.</p>', status_code=400)

    db.collection("contact_messages").add({
        "order_id": order_id, "name": name, "email": email, "message": message,
        "created_at": firestore.SERVER_TIMESTAMP,
    })
    # No email/SMS service is wired up yet - this is the admin notification
    # for now. Once one exists, replace this with a real page/alert.
    print(f"📬 New contact message from {name} <{email}> (order {order_id}): {message}")

    return templates.TemplateResponse(request, "contact_result.html", {})

@app.get("/download-brand-kit/{order_id}")
async def download_brand_kit(order_id: str):
    order = ORDERS.document(order_id).get().to_dict()
    if not order or not order.get("brand_result"):
        return HTMLResponse("Not found", status_code=404)

    business_name = order.get("business_name", "business")
    safe_name = business_name.replace(" ", "_").replace("/", "_")
    return Response(
        content=order["brand_result"].get("result", ""),
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={safe_name}_Brand_Kit.txt"},
    )

@app.get("/download-certificate/{order_id}")
async def download_certificate(order_id: str):
    order = ORDERS.document(order_id).get().to_dict()
    if not order or not order.get("certificate_uploaded_at"):
        return HTMLResponse("Not found", status_code=404)

    try:
        pdf_bytes = fetch_certificate(order_id)
    except Exception as e:
        print(f"⚠️ Could not fetch certificate for order {order_id}: {e}")
        return HTMLResponse("Not found", status_code=404)

    business_name = order.get("business_name", "business")
    safe_name = business_name.replace(" ", "_").replace("/", "_")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={safe_name}_Certificate.pdf"},
    )

@app.get("/download-ein-letter/{order_id}")
async def download_ein_letter(order_id: str):
    order = ORDERS.document(order_id).get().to_dict()
    if not order or not order.get("ein_letter_uploaded_at"):
        return HTMLResponse("Not found", status_code=404)

    try:
        pdf_bytes = fetch_ein_letter(order_id)
    except Exception as e:
        print(f"⚠️ Could not fetch EIN letter for order {order_id}: {e}")
        return HTMLResponse("Not found", status_code=404)

    business_name = order.get("business_name", "business")
    safe_name = business_name.replace(" ", "_").replace("/", "_")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={safe_name}_EIN_Confirmation.pdf"},
    )

@app.get("/download-pdf/{filename}")
async def download_pdf(filename: str):
    filepath = f"app/static/docs/{filename}"
    if os.path.exists(filepath):
        return FileResponse(
            path=filepath,
            media_type="application/pdf",
            filename=filename,
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    return {"error": "File not found"}

@app.get("/health")
def health():
    return {"status": "ok"}
