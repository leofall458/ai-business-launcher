import os
import secrets
import asyncio
import datetime
from google.cloud import firestore
from fastapi import FastAPI, Request, BackgroundTasks, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from app.config import FIREBASE_PROJECT_ID, ADMIN_PASSWORD
from app.agents.name_agent import screen_business_name
from app.agents.name_check_agent import check_business_name
from app.agents.scc_name_check import check_name_on_scc
from app.agents.llc_agent import generate_llc_paperwork
from app.agents.brand_agent import generate_brand_kit
from app.agents.marketing_agent import generate_marketing_plan
from app.agents.pdf_agent import generate_llc_pdf
from app.scc_llc_filer import file_llc_on_scc
from app.ein_filer import file_ein_with_irs
from app.agents.website_agent import generate_website_content, render_website_html
from app.deployer import deploy_website
from app.stripe_service import (
    create_checkout_session,
    retrieve_checkout_session,
    create_connect_account,
    create_account_link,
    create_pay_what_you_want_payment_link,
)
from app.ssn_cache import stash as stash_ssn, peek as peek_ssn, discard as discard_ssn

app = FastAPI(title="Launch Bridge LLC")

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

db = firestore.Client(project=FIREBASE_PROJECT_ID)

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

STATE_MESSAGES = {
    "draft": "We're waiting for your payment to go through.",
    "paid": "Payment received. We're checking your business name's availability with the Virginia State Corporation Commission.",
    "name_cleared": "Your business name is available. Your order is queued for review before we file.",
    "review_approved": "Your order has been approved and we're preparing your filing.",
    "filing_submitted": "Your LLC paperwork has been submitted to the Virginia SCC and your EIN application is underway. SCC approval typically takes several business days - we don't control their timeline and can't promise an exact date.",
    "filing_confirmed": "Your Virginia LLC has been officially approved by the SCC.",
    "ein_requested": "Your EIN application has been submitted to the IRS.",
    "ein_issued": "Your EIN has been issued. We're now generating your brand kit, marketing plan, and business website.",
    "assets_generated": "Your brand kit, marketing plan, and business website are ready.",
    "complete": "Everything is ready - here's your full business package.",
}

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
    )

    return templates.TemplateResponse(request, "name_check_result.html", {
        "result": gemini_result,
        "scc": scc_result,
        "desired_name": desired_name
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
    act on (the automated pipeline doesn't know how to fix a taken name)."""
    order_ref = db.collection("orders").document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return

    try:
        result = check_name_on_scc(order["business_name"])
        order_ref.set({"name_check": result}, merge=True)
        if result.get("available"):
            order_ref.set({"state": "name_cleared"}, merge=True)
        else:
            print(f"⚠️ Name check did not clear for order {order_id}: {result.get('message')}")
    except Exception as e:
        print(f"⚠️ Name check crashed for order {order_id}: {e}")
        order_ref.set({"name_check_error": str(e)}, merge=True)

SCC_FILED_STATES = {"filing_submitted", "filing_confirmed", "ein_requested", "ein_issued", "assets_generated", "complete"}

def run_scc_and_ein_filing(order_id: str):
    """Triggered by the admin's Approve button. Files the LLC with SCC,
    then - per standing business decision - fires the EIN application
    immediately once the LLC paperwork is filled, without waiting for SCC's
    actual approval (which can take days).

    Re-entrant: if the order already shows filing_submitted or later (e.g.
    the admin re-clicks Approve to retry a failed EIN attempt - the IRS
    online assistant is only open 7am-10pm ET, so this will happen), the
    SCC step is skipped entirely rather than re-filing with SCC a second
    time. The SSN is only read (peeked) from the in-memory cache, and is
    only discarded for good once EIN filing actually succeeds - a failed
    attempt must leave it available for the next retry. It never touches
    Firestore either way. If the server restarts before EIN succeeds, the
    SSN is lost and the customer must be asked for it again; that's the
    accepted tradeoff for never persisting it.

    Wrapped in a single try/except: if either filer crashes outright
    (closed tab, site change, network blip) rather than returning False,
    the order must not be left stuck with no visible explanation - that
    would defeat the point of an admin-visible state machine."""
    order_ref = db.collection("orders").document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return

    already_filed_with_scc = order.get("state") in SCC_FILED_STATES

    try:
        if not already_filed_with_scc:
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
            }
            llc_filed = file_llc_on_scc(llc_customer_data, interactive=False)
            if not llc_filed:
                order_ref.set({"filing_error": "SCC filing did not complete - check server screenshots, then re-approve to retry."}, merge=True)
                return

            order_ref.set({"state": "filing_submitted", "filing_error": firestore.DELETE_FIELD}, merge=True)
            already_filed_with_scc = True

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
            order_ref.set({"state": "ein_requested", "ein_error": firestore.DELETE_FIELD}, merge=True)
        else:
            order_ref.set({"ein_error": "EIN filing did not complete - the IRS online assistant is only available 7am-10pm ET, so this may just be timing. Re-approve to retry."}, merge=True)
    except Exception as e:
        print(f"⚠️ SCC/EIN filing crashed for order {order_id}: {e}")
        error_field = "ein_error" if already_filed_with_scc else "filing_error"
        order_ref.set({error_field: f"Filing crashed unexpectedly: {e}. Check server logs/screenshots."}, merge=True)

def run_asset_generation(order_id: str):
    """Triggered once the admin records the real EIN. Generates the brand
    kit, marketing plan, name ideas, and signed LLC PDF, sets up the
    customer's Stripe Connect Standard account and a pay-what-you-want
    Payment Link on it, then deploys the business website with that link
    already embedded (falling back to a mailto link if Connect setup
    fails, same as before)."""
    order_ref = db.collection("orders").document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return

    try:
        business_name = order["business_name"]
        full_name = order["full_name"]
        business_purpose = order["business_purpose"]
        principal_address = order["principal_address"]
        primary_sig = order["primary_sig"]
        business_idea = order["business_idea"]
        target_customer = order["target_customer"]
        template_style = order.get("template_style", "auto")
        email = order["email"]
        phone = order["phone"]

        name_result = screen_business_name(business_idea)
        brand_result = generate_brand_kit(business_name, business_idea, target_customer)
        marketing_result = generate_marketing_plan(business_name, business_idea, "Virginia", target_customer)

        pdf_path = generate_llc_pdf(business_name, full_name, business_purpose, full_name, principal_address, primary_sig)
        safe_name = business_name.replace(" ", "_").replace("/", "_")
        pdf_filename = f"{safe_name}_LLC_Package.pdf"

        connect_account_id = None
        payment_link_url = None
        try:
            account = create_connect_account(
                email=email,
                first_name=order["first_name"],
                last_name=order["last_name"],
                business_name=business_name,
                multi_member=len(order.get("all_signatures", [])) > 1,
            )
            connect_account_id = account.id
            payment_link_url = create_pay_what_you_want_payment_link(account.id, business_name)
        except Exception as e:
            print(f"⚠️ Could not set up Stripe Connect for order {order_id}: {e}")

        website_url = None
        try:
            content = generate_website_content(business_name, business_idea, target_customer)
            template_override = None if template_style == "auto" else template_style
            html, template_name = render_website_html(
                content, business_name, email, phone, principal_address,
                template_override=template_override, payment_link_url=payment_link_url,
            )
            deployed = deploy_website(business_name, html, order_id=order_id)
            if deployed:
                website_url = deployed["url"]
                order_ref.set({"website_template": template_name, "website_content": content}, merge=True)
        except Exception as e:
            print(f"⚠️ Website generation/deploy failed for order {order_id}: {e}")

        order_ref.set({
            "name_result": name_result,
            "brand_result": brand_result,
            "marketing_result": marketing_result,
            "pdf_filename": pdf_filename,
            "website_url": website_url,
            "stripe_connect_account_id": connect_account_id,
            "state": "assets_generated",
        }, merge=True)

        order_ref.set({"state": "complete"}, merge=True)
    except Exception as e:
        print(f"⚠️ Asset generation crashed for order {order_id}: {e}")
        order_ref.set({"asset_generation_error": f"Asset generation crashed unexpectedly: {e}. Check server logs."}, merge=True)

@app.post("/launch", response_class=HTMLResponse)
async def launch(request: Request):
    form_raw = await request.form()
    form = dict(form_raw)
    ssn = form.pop("ssn", "")

    parsed = parse_intake_form(form)

    order_ref = db.collection("orders").document()
    order_id = order_ref.id
    order_ref.set({
        **form,
        **parsed,
        "state": "draft",
        "created_at": firestore.SERVER_TIMESTAMP,
    })

    if ssn:
        stash_ssn(order_id, ssn)

    base_url = str(request.base_url)
    session = create_checkout_session(
        order_id=order_id,
        business_name=parsed["business_name"],
        success_url=f"{base_url}success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}cancel",
    )

    return Response(status_code=200, headers={"HX-Redirect": session.url})

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

        order_ref = db.collection("orders").document(resolved_order_id)
        order_snap = order_ref.get()
        if not order_snap.exists:
            return HTMLResponse("<p>Order not found.</p>", status_code=404)
        order = order_snap.to_dict()

        if order.get("state") == "draft" and session.payment_status == "paid":
            order_ref.set({"state": "paid"}, merge=True)
            background_tasks.add_task(run_name_check, resolved_order_id)

    if not resolved_order_id:
        return RedirectResponse(url="/")

    return RedirectResponse(url=f"/status/{resolved_order_id}")

@app.get("/status/{order_id}", response_class=HTMLResponse)
async def status_page(request: Request, order_id: str):
    order_snap = db.collection("orders").document(order_id).get()
    if not order_snap.exists:
        return HTMLResponse("<p>Order not found.</p>", status_code=404)
    order = order_snap.to_dict()
    state = order.get("state", "draft")

    return templates.TemplateResponse(request, "status.html", {
        "order_id": order_id,
        "state": state,
        "state_message": STATE_MESSAGES.get(state, ""),
        "ORDER_STATES": ORDER_STATES,
        "business_name": order.get("business_name"),
        "full_name": order.get("full_name"),
        "name_check": order.get("name_check"),
        "filing_error": order.get("filing_error"),
        "ein_error": order.get("ein_error"),
        "asset_generation_error": order.get("asset_generation_error"),
        "ein": order.get("ein"),
        "website_url": order.get("website_url"),
        "pdf_filename": order.get("pdf_filename"),
        "name_result": order.get("name_result"),
        "brand_result": order.get("brand_result"),
        "marketing_result": order.get("marketing_result"),
        "onboarding_url": f"/connect/onboard/{order_id}" if order.get("stripe_connect_account_id") else None,
    })

@app.get("/cancel")
async def cancel():
    return RedirectResponse(url="/?cancelled=1")

@app.get("/connect/onboard/{order_id}")
async def connect_onboard(request: Request, order_id: str):
    order = db.collection("orders").document(order_id).get().to_dict()
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
    query = db.collection("orders").order_by("created_at", direction=firestore.Query.DESCENDING)
    for doc in query.stream():
        order = doc.to_dict()
        order["id"] = doc.id
        orders.append(order)

    return templates.TemplateResponse(request, "admin.html", {
        "orders": orders,
    })

@app.post("/admin/{order_id}/approve")
async def admin_approve(order_id: str, background_tasks: BackgroundTasks, authorized: bool = Depends(verify_admin)):
    db.collection("orders").document(order_id).set({"state": "review_approved"}, merge=True)
    background_tasks.add_task(run_scc_and_ein_filing, order_id)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/{order_id}/mark-filed")
async def admin_mark_filed(order_id: str, authorized: bool = Depends(verify_admin)):
    db.collection("orders").document(order_id).set({
        "state": "filing_confirmed", "filing_error": firestore.DELETE_FIELD,
    }, merge=True)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/{order_id}/mark-ein")
async def admin_mark_ein(order_id: str, background_tasks: BackgroundTasks, ein: str = Form(...), authorized: bool = Depends(verify_admin)):
    db.collection("orders").document(order_id).set({
        "ein": ein, "state": "ein_issued",
        "ein_error": firestore.DELETE_FIELD, "asset_generation_error": firestore.DELETE_FIELD,
    }, merge=True)
    background_tasks.add_task(run_asset_generation, order_id)
    return RedirectResponse(url="/admin", status_code=303)

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
