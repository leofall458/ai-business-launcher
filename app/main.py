import os
import secrets
import asyncio
import datetime
from urllib.parse import quote
from google.cloud import firestore
from fastapi import FastAPI, Request, BackgroundTasks, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from app.config import FIREBASE_PROJECT_ID, ADMIN_PASSWORD, ORDERS_COLLECTION
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
from app.ssn_cache import stash as stash_ssn, peek as peek_ssn, discard as discard_ssn
from app.validators import validate_intake_form, validate_ssn, ALL_VALIDATED_FIELDS
from app.email_service import (
    send_order_received_email,
    send_documents_ready_email,
    send_llc_filed_email,
    send_llc_approved_email,
    send_ein_issued_email,
    send_website_live_email,
    send_everything_complete_email,
    send_order_id_email,
)
from app.storage_service import fetch_certificate

app = FastAPI(title="Launch Bridge LLC")

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

db = firestore.Client(project=FIREBASE_PROJECT_ID)
ORDERS = db.collection(ORDERS_COLLECTION)

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

# Canonical order of the order state machine. An order's "state" field is
# always one of these. Progression is mostly linear, but filing_confirmed
# and ein_requested/ein_issued can land out of this textbook order in real
# wall-clock time (SCC and the IRS move at their own pace) - the admin
# dashboard lets the admin set filing_confirmed and ein_issued independently
# of where the automated steps currently are.
ORDER_STATES = [
    "draft", "paid", "name_cleared", "review_approved", "filing_submitted",
    "filing_confirmed", "ein_requested", "ein_issued", "assets_generated", "complete",
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
    if state == "filing_confirmed":
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

    if not order.get("brand_result"):
        try:
            update["brand_result"] = generate_brand_kit(business_name, business_idea, target_customer)
        except Exception as e:
            print(f"⚠️ Brand kit agent failed for order {order_id}: {e}")
            errors["brand_result"] = f"Brand kit: {e}"

    if not order.get("marketing_result"):
        try:
            update["marketing_result"] = generate_marketing_plan(business_name, business_idea, "Virginia", target_customer)
        except Exception as e:
            print(f"⚠️ Marketing plan agent failed for order {order_id}: {e}")
            errors["marketing_result"] = f"Marketing plan: {e}"

    if not order.get("pdf_filename"):
        try:
            generate_llc_pdf(business_name, full_name, business_purpose, full_name, principal_address, primary_sig)
            safe_name = business_name.replace(" ", "_").replace("/", "_")
            update["pdf_filename"] = f"{safe_name}_LLC_Package.pdf"
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
        update.get("pdf_filename") or order.get("pdf_filename"),
    ])
    newly_generated = have_all and not order.get("documents_generated")
    update["documents_generated"] = have_all
    if have_all:
        update["documents_generated_at"] = firestore.SERVER_TIMESTAMP
    update["documents_error"] = "; ".join(errors.values()) if errors else firestore.DELETE_FIELD
    order_ref.set(update, merge=True)

    if newly_generated:
        send_documents_ready_email(order, order_id)

SCC_FILED_STATES = {"filing_submitted", "filing_confirmed", "ein_requested", "ein_issued", "assets_generated", "complete"}

# An order is only eligible to have an EIN application filed once the SCC
# has actually confirmed the LLC (filing_confirmed) - we no longer fire EIN
# the moment paperwork is filed, since SCC approval can take days and the
# IRS step should reflect a real, confirmed entity.
EIN_ELIGIBLE_STATES = {"filing_confirmed"}

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
        return True

    extra = {"filing_confirmed_at": firestore.SERVER_TIMESTAMP, "filing_error": firestore.DELETE_FIELD}
    if is_irs_open():
        extra["ein_status"] = firestore.DELETE_FIELD
        extra["next_available_window"] = firestore.DELETE_FIELD
    else:
        window = next_irs_open()
        extra["ein_status"] = "queued"
        extra["next_available_window"] = window.isoformat()
    record_state(order_ref, "filing_confirmed", **extra)
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

def run_ein_filing(order_id: str):
    """Triggered by the admin's Apply for EIN button - only reachable once
    the order is filing_confirmed (SCC's real approval). Re-checks IRS
    business hours (Mon-Fri 7am-10pm ET) itself, in addition to the route's
    own check, to cover the race where hours close between the click and
    this background task actually running.

    The SSN is only read (peeked) from the in-memory cache, and is only
    discarded for good once EIN filing actually succeeds - a failed or
    blocked attempt must leave it available for the next retry. If the
    server restarts before EIN succeeds, the SSN is lost and the customer
    must be asked for it again; that's the accepted tradeoff for never
    persisting it to Firestore."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
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

    try:
        ssn = peek_ssn(order_id)
        if not ssn:
            order_ref.set({
                "ein_error": "SSN is no longer in memory (server likely restarted since payment, or EIN was already filed once). The customer must be contacted to resubmit it before EIN filing can proceed."
            }, merge=True)
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
        ein_filed = file_ein_with_irs(ein_customer_data, interactive=False)
        if ein_filed:
            discard_ssn(order_id)
            record_state(order_ref, "ein_requested",
                ein_requested_at=firestore.SERVER_TIMESTAMP,
                ein_error=firestore.DELETE_FIELD,
                ein_status=firestore.DELETE_FIELD,
                next_available_window=firestore.DELETE_FIELD,
            )
        else:
            order_ref.set({"ein_error": "EIN filing did not complete - check server screenshots, then apply again to retry."}, merge=True)
    except Exception as e:
        print(f"⚠️ EIN filing crashed for order {order_id}: {e}")
        order_ref.set({"ein_error": f"Filing crashed unexpectedly: {e}. Check server logs/screenshots."}, merge=True)

async def ein_queue_scheduler():
    """Runs for the lifetime of the process, woken every 5 minutes. Picks
    up any order stuck at filing_confirmed with ein_status="queued" (set
    either by mark-filed or a blocked Apply for EIN click) and fires the
    EIN filing the moment IRS hours actually open - so a queued order
    doesn't just sit there until an admin happens to click again."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            if is_irs_open():
                query = (
                    ORDERS
                    .where("state", "==", "filing_confirmed")
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
                payment_link_url = create_pay_what_you_want_payment_link(account.id, business_name)
            except Exception as e:
                print(f"⚠️ Could not set up Stripe Connect for order {order_id}: {e}")
                asset_error = f"Could not set up your Stripe payment account: {e}"

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
            send_everything_complete_email(order, order_id)
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
    the SSN not been needed."""
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return

    order_ref.set({"awaiting_ssn": False, "ssn_collected_at": firestore.SERVER_TIMESTAMP}, merge=True)

    if order.get("skip_llc_formation"):
        background_tasks.add_task(run_document_generation, order_id)
        trigger_assets = advance_past_filing_confirmed(order_ref, order)
        if trigger_assets:
            background_tasks.add_task(run_asset_generation, order_id)
    else:
        background_tasks.add_task(run_name_check, order_id)

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
    if order and order.get("awaiting_ssn"):
        return RedirectResponse(url=f"/collect-ssn/{resolved_order_id}")

    return RedirectResponse(url=f"/status/{resolved_order_id}")

@app.get("/collect-ssn/{order_id}", response_class=HTMLResponse)
async def collect_ssn_page(request: Request, order_id: str):
    order = ORDERS.document(order_id).get().to_dict()
    if not order:
        return HTMLResponse("<p>Order not found.</p>", status_code=404)
    if not order.get("awaiting_ssn"):
        return RedirectResponse(url=f"/status/{order_id}")

    return templates.TemplateResponse(request, "collect_ssn.html", {
        "order_id": order_id,
        "business_name": order.get("business_name", ""),
        "error": None,
    })

@app.post("/collect-ssn/{order_id}", response_class=HTMLResponse)
async def collect_ssn_submit(request: Request, order_id: str, background_tasks: BackgroundTasks):
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return HTMLResponse("<p>Order not found.</p>", status_code=404)
    if not order.get("awaiting_ssn"):
        return RedirectResponse(url=f"/status/{order_id}")

    form = await request.form()
    ssn = (form.get("ssn") or "").strip()
    error = validate_ssn(ssn)
    if error:
        return templates.TemplateResponse(request, "collect_ssn.html", {
            "order_id": order_id,
            "business_name": order.get("business_name", ""),
            "error": error,
        }, status_code=400)

    # In-memory only, same as the old intake-time flow - never written to
    # Firestore. See app/ssn_cache.py.
    stash_ssn(order_id, ssn)
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

    # Filtered by timestamp in Python rather than a second Firestore
    # inequality clause - that would need a composite index, and request
    # volume per email is small enough that fetching-then-filtering is
    # simpler to ship and just as correct.
    request_log = db.collection("order_id_requests")
    window_start = datetime.datetime.now(datetime.timezone.utc) - ORDER_ID_REQUEST_WINDOW
    same_email_requests = request_log.where("email", "==", email).stream()
    recent = [r for r in same_email_requests if (r.to_dict().get("requested_at") or window_start) > window_start]
    if len(recent) >= ORDER_ID_REQUEST_LIMIT:
        return HTMLResponse(
            '<p class="text-yellow-400 text-sm mt-2">Too many requests for this email — please try again in an '
            'hour, or contact <a href="mailto:support@launchbridge.ai" class="underline">support@launchbridge.ai</a>.</p>',
            status_code=429,
        )
    request_log.add({"email": email, "requested_at": firestore.SERVER_TIMESTAMP})

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

@app.get("/status/{order_id}", response_class=HTMLResponse)
async def status_page(request: Request, order_id: str):
    order_snap = ORDERS.document(order_id).get()
    if not order_snap.exists:
        return templates.TemplateResponse(request, "order_not_found.html", {}, status_code=404)
    order = order_snap.to_dict()
    state = order.get("state", "draft")

    if order.get("awaiting_ssn"):
        return RedirectResponse(url=f"/collect-ssn/{order_id}")

    return templates.TemplateResponse(request, "status.html", {
        **status_context(order_id, order),
        "business_name": order.get("business_name"),
        "full_name": order.get("full_name"),
        "email": order.get("email"),
        "registered_agent_choice": order.get("registered_agent_choice", "launchbridge"),
        "ein": order.get("ein"),
        "website_url": order.get("website_url"),
        "website_template": order.get("website_template"),
        "documents_generated": order.get("documents_generated", False),
        "pdf_filename": order.get("pdf_filename"),
        "has_brand_kit": bool(order.get("brand_result")),
        "filing_confirmed": reached(state, "filing_confirmed") or bool(order.get("skip_llc_formation")),
        "scc_confirmation_number": order.get("scc_confirmation_number"),
        "has_certificate": bool(order.get("certificate_uploaded_at")),
        "onboarding_url": f"/connect/onboard/{order_id}" if order.get("stripe_connect_account_id") else None,
    })

@app.get("/status/{order_id}/timeline", response_class=HTMLResponse)
async def status_timeline_partial(request: Request, order_id: str):
    """Polled every 30s by the status page (see status.html) to refresh
    just the timeline + estimate, without reloading documents/contact/share
    sections that don't change nearly as often."""
    order_snap = ORDERS.document(order_id).get()
    if not order_snap.exists:
        return HTMLResponse("")
    order = order_snap.to_dict()

    return templates.TemplateResponse(request, "status_timeline.html", {
        **status_context(order_id, order),
        "onboarding_url": f"/connect/onboard/{order_id}" if order.get("stripe_connect_account_id") else None,
    })

@app.get("/cancel")
async def cancel():
    return RedirectResponse(url="/?cancelled=1")

@app.get("/connect/onboard/{order_id}")
async def connect_onboard(request: Request, order_id: str):
    order = ORDERS.document(order_id).get().to_dict()
    if not order or not order.get("stripe_connect_account_id"):
        return RedirectResponse(url="/")

    base_url = str(request.base_url)
    url = create_account_link(
        order["stripe_connect_account_id"],
        refresh_url=f"{base_url}connect/onboard/{order_id}",
        return_url=f"{base_url}status/{order_id}",
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

@app.post("/admin/{order_id}/mark-ein")
async def admin_mark_ein(order_id: str, background_tasks: BackgroundTasks, ein: str = Form(...), authorized: bool = Depends(verify_admin)):
    order_ref = ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    record_state(order_ref, "ein_issued",
        ein=ein, ein_issued_at=firestore.SERVER_TIMESTAMP,
        ein_error=firestore.DELETE_FIELD, asset_generation_error=firestore.DELETE_FIELD,
    )
    if order:
        send_ein_issued_email(order, order_id, ein)
    background_tasks.add_task(run_asset_generation, order_id)
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
