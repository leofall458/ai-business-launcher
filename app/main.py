import os
import asyncio
import datetime
from google.cloud import firestore
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from app.config import FIREBASE_PROJECT_ID
from app.agents.name_agent import screen_business_name
from app.agents.name_check_agent import check_business_name
from app.agents.scc_name_check import check_name_on_scc
from app.agents.llc_agent import generate_llc_paperwork
from app.agents.brand_agent import generate_brand_kit
from app.agents.marketing_agent import generate_marketing_plan
from app.agents.ein_agent import generate_ein_guidance
from app.agents.pdf_agent import generate_llc_pdf
from app.scc_llc_filer import file_llc_on_scc
from app.ein_filer import file_ein_with_irs
from app.agents.website_agent import generate_website, render_website_html
from app.deployer import deploy_website, update_index_html
from app.stripe_service import (
    create_checkout_session,
    retrieve_checkout_session,
    create_connect_account,
    create_account_link,
    create_pay_what_you_want_payment_link,
)
from app.ssn_cache import stash as stash_ssn, pop as pop_ssn

app = FastAPI(title="Launch Bridge LLC")

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

db = firestore.Client(project=FIREBASE_PROJECT_ID)

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
    description) and again in /success when re-reading the same fields
    back out of the Firestore order document."""
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

def build_and_deploy_website(business_name, business_idea, target_customer, email, phone, address, template_style, order_id):
    """Generates tailored website copy, renders it into the chosen template,
    and deploys it to its own GitHub Pages site. Returns the live URL, or
    None if generation/deployment failed. The Pay Now button starts out as
    a mailto fallback - it gets swapped for a real Stripe Payment Link once
    the customer's Connect account exists (see setup_connect_and_payment_link)."""
    template_override = None if template_style == "auto" else template_style
    try:
        result = generate_website(
            business_name, business_idea, target_customer, email, phone, address,
            template_override=template_override
        )
    except Exception as e:
        print(f"⚠️ Website content generation failed: {e}")
        return None

    deployed = deploy_website(business_name, result["html"], order_id=order_id)
    if not deployed:
        return None

    try:
        db.collection("orders").document(order_id).set({
            "website_template": result["template"],
            "website_content": result["content"],
        }, merge=True)
    except Exception as e:
        print(f"⚠️ Could not save website content to Firestore order {order_id}: {e}")

    return deployed["url"]

def run_llc_and_ein_filing(llc_customer_data: dict, ein_customer_data: dict):
    """Fires the SCC LLC filer, then the IRS EIN filer right after the LLC
    paperwork is filled - not waiting for SCC to actually approve the LLC.
    Both filers stop before final payment/submission and leave their browser
    tab open on the Review page for manual completion."""
    llc_filled = file_llc_on_scc(llc_customer_data, interactive=False)
    if llc_filled:
        file_ein_with_irs(ein_customer_data, interactive=False)
    else:
        print("⚠️ Skipping EIN filing - LLC filing did not complete successfully")

def setup_connect_and_payment_link(order_id: str, base_url: str) -> dict:
    """Part 2 + 3: create the customer's Stripe Connect Express account
    (pre-filled as an LLC), then - since their website is already live -
    create a Payment Link on that account and push it into their site,
    replacing the mailto fallback. Returns the onboarding entry point for
    the results page; the actual Account Link is minted fresh on each visit
    to /connect/onboard since Stripe's links expire in minutes, not the 24
    hours one might assume."""
    order_ref = db.collection("orders").document(order_id)
    order = order_ref.get().to_dict() or {}

    try:
        account = create_connect_account(
            email=order.get("email", ""),
            first_name=order.get("first_name", ""),
            last_name=order.get("last_name", ""),
            business_name=order.get("business_name", ""),
            multi_member=len(order.get("all_signatures", [])) > 1,
        )
    except Exception as e:
        print(f"⚠️ Could not create Stripe Connect account for order {order_id}: {e}")
        return {"connect_account_id": None, "onboarding_url": None}

    order_ref.set({"stripe_connect_account_id": account.id}, merge=True)

    website_repo = order.get("website_repo")
    if website_repo:
        try:
            payment_link_url = create_pay_what_you_want_payment_link(account.id, order.get("business_name", ""))
            template_name = order.get("website_template")
            content = order.get("website_content")
            if template_name and content:
                html, _ = render_website_html(
                    content, order.get("business_name", ""), order.get("email", ""),
                    order.get("phone", ""), order.get("principal_address", ""),
                    template_override=template_name, payment_link_url=payment_link_url,
                )
                update_index_html(website_repo, html)
                order_ref.set({"payment_link_url": payment_link_url}, merge=True)
        except Exception as e:
            print(f"⚠️ Could not create/embed Payment Link for order {order_id}: {e}")

    return {
        "connect_account_id": account.id,
        "onboarding_url": f"{base_url}connect/onboard/{order_id}",
    }

async def process_paid_order(order_id: str, base_url: str, background_tasks: BackgroundTasks) -> dict:
    """Runs every AI agent for a now-paid order, deploys the website, kicks
    off the slow Playwright-driven LLC+EIN filing in the background, then
    sets up Stripe Connect. Returns the results-page context and stores it
    on the order doc so a /success refresh re-displays it instead of
    re-running everything (and re-charging nothing, but re-filing things)."""
    order_ref = db.collection("orders").document(order_id)
    order = order_ref.get().to_dict()

    business_name = order["business_name"]
    full_name = order["full_name"]
    business_purpose = order["business_purpose"]
    principal_address = order["principal_address"]
    primary_sig = order["primary_sig"]
    all_signatures = order["all_signatures"]
    email = order["email"]
    phone = order["phone"]
    business_idea = order["business_idea"]
    target_customer = order["target_customer"]
    template_style = order["template_style"]
    first_name = order["first_name"]
    middle_name = order.get("middle_name", "")
    last_name = order["last_name"]
    address = order["address"]
    city = order["city"]
    county = order["county"]
    zipcode = order["zipcode"]
    industry_code = order["industry_code"]
    duration = order["duration"]
    dob = order.get("dob", "")

    loop = asyncio.get_event_loop()

    (
        name_result,
        llc_result,
        ein_result,
        brand_result,
        marketing_result,
        website_url
    ) = await asyncio.gather(
        loop.run_in_executor(None, screen_business_name, business_idea),
        loop.run_in_executor(None, generate_llc_paperwork,
            business_name, full_name, business_purpose, full_name, principal_address),
        loop.run_in_executor(None, generate_ein_guidance,
            business_name, full_name, "Virginia"),
        loop.run_in_executor(None, generate_brand_kit,
            business_name, business_idea, target_customer),
        loop.run_in_executor(None, generate_marketing_plan,
            business_name, business_idea, "Virginia", target_customer),
        loop.run_in_executor(None, build_and_deploy_website,
            business_name, business_idea, target_customer, email, phone, principal_address, template_style, order_id),
    )

    pdf_path = await loop.run_in_executor(None, generate_llc_pdf,
        business_name, full_name, business_purpose, full_name, principal_address, primary_sig)

    safe_name = business_name.replace(" ", "_").replace("/", "_")
    pdf_filename = f"{safe_name}_LLC_Package.pdf"

    today = datetime.date.today()
    ssn = pop_ssn(order_id)
    llc_customer_data = {
        "business_name": business_name,
        "first_name": first_name,
        "middle_name": middle_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
        "address": address,
        "city": city,
        "zipcode": zipcode,
        "industry_code": industry_code,
        "duration": duration,
    }
    ein_customer_data = {
        "business_name": business_name,
        "first_name": first_name,
        "middle_name": middle_name,
        "last_name": last_name,
        "ssn": ssn,
        "address": address,
        "city": city,
        "state": "VA",
        "zipcode": zipcode,
        "phone": phone,
        "county": county,
        "start_month": today.strftime("%B"),
        "start_year": str(today.year),
        "members": str(len(all_signatures)),
        "business_description": business_purpose,
    }
    background_tasks.add_task(run_llc_and_ein_filing, llc_customer_data, ein_customer_data)

    connect_result = await loop.run_in_executor(None, setup_connect_and_payment_link, order_id, base_url)

    context = {
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "dob": dob,
        "business_name": business_name,
        "principal_address": principal_address,
        "industry_code": industry_code,
        "duration": duration,
        "all_signatures": all_signatures,
        "name_result": name_result,
        "llc_result": llc_result,
        "ein_result": ein_result,
        "brand_result": brand_result,
        "marketing_result": marketing_result,
        "pdf_filename": pdf_filename,
        "website_url": website_url,
        "onboarding_url": connect_result.get("onboarding_url"),
    }

    order_ref.set({"status": "complete", "results": context}, merge=True)

    return context

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
        "status": "pending_payment",
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

@app.get("/success", response_class=HTMLResponse)
async def success(request: Request, background_tasks: BackgroundTasks, session_id: str = None, order_id: str = None):
    resolved_order_id = order_id
    session = None

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

    if order.get("status") == "complete":
        return templates.TemplateResponse(request, "success.html", order.get("results", {}))

    if not session or session.payment_status != "paid":
        return HTMLResponse("<p>Payment not completed.</p>", status_code=402)

    order_ref.set({"status": "paid"}, merge=True)

    base_url = str(request.base_url)
    context = await process_paid_order(resolved_order_id, base_url, background_tasks)

    return templates.TemplateResponse(request, "success.html", context)

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
        return_url=f"{base_url}success?order_id={order_id}",
    )
    return RedirectResponse(url=url)

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
