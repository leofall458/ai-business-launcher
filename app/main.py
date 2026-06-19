import os
import asyncio
import datetime
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
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

app = FastAPI(title="Launch Bridge LLC")

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html")

@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse(request, "terms.html")

@app.post("/screen-name", response_class=HTMLResponse)
async def screen_name(
    request: Request,
    business_idea: str = Form(...)
):
    result = screen_business_name(business_idea)
    return templates.TemplateResponse(request, "result.html", {"result": result})

@app.post("/check-name", response_class=HTMLResponse)
async def check_name(
    request: Request,
    desired_name: str = Form(...)
):
    loop = asyncio.get_event_loop()

    # Run SCC check and Gemini analysis in parallel
    scc_result, gemini_result = await asyncio.gather(
        loop.run_in_executor(None, check_name_on_scc, desired_name),
        loop.run_in_executor(None, check_business_name, desired_name, "Virginia"),
    )

    return templates.TemplateResponse(request, "name_check_result.html", {
        "result": gemini_result,
        "scc": scc_result,
        "desired_name": desired_name
    })

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

@app.post("/launch", response_class=HTMLResponse)
async def launch(request: Request, background_tasks: BackgroundTasks):
    form = await request.form()

    first_name = form.get("first_name", "")
    middle_name = form.get("middle_name", "")
    last_name = form.get("last_name", "")
    email = form.get("email", "")
    phone = form.get("phone", "")
    dob = form.get("dob", "")
    address = form.get("address", "")
    city = form.get("city", "")
    county = form.get("county", "")
    zipcode = form.get("zipcode", "")
    ssn = form.get("ssn", "")
    business_idea = form.get("business_idea", "")
    desired_name = form.get("desired_name", "")
    business_purpose = form.get("business_purpose", "")
    target_customer = form.get("target_customer", "")
    industry_code = form.get("industry_code", "0")
    duration = form.get("duration", "Perpetual")
    sig_first = form.get("sig_first", "")
    sig_middle = form.get("sig_middle", "")
    sig_last = form.get("sig_last", "")

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
        sig = f"{first} {middle} {last}".replace("  ", " ").strip()
        additional_members.append(sig)
        i += 1

    all_signatures = [primary_sig] + additional_members
    business_name = desired_name.strip() if desired_name.strip() else f"{last_name} Ventures LLC"

    loop = asyncio.get_event_loop()

    (
        name_result,
        llc_result,
        ein_result,
        brand_result,
        marketing_result
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
    )

    pdf_path = await loop.run_in_executor(None, generate_llc_pdf,
        business_name, full_name, business_purpose, full_name, principal_address, primary_sig)

    safe_name = business_name.replace(" ", "_").replace("/", "_")
    pdf_filename = f"{safe_name}_LLC_Package.pdf"

    today = datetime.date.today()
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

    return templates.TemplateResponse(request, "launch_result.html", {
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "dob": dob,
        "ssn": ssn,
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
        "pdf_filename": pdf_filename
    })

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
