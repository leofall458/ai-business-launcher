import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from app.agents.name_agent import screen_business_name
from app.agents.name_check_agent import check_business_name
from app.agents.llc_agent import generate_llc_paperwork
from app.agents.brand_agent import generate_brand_kit
from app.agents.marketing_agent import generate_marketing_plan
from app.agents.ein_agent import generate_ein_guidance
from app.agents.pdf_agent import generate_llc_pdf

app = FastAPI(title="AI Business Launcher")

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.post("/launch", response_class=HTMLResponse)
async def launch(request: Request):
    form = await request.form()

    # Primary member info
    first_name = form.get("first_name", "")
    middle_name = form.get("middle_name", "")
    last_name = form.get("last_name", "")
    email = form.get("email", "")
    phone = form.get("phone", "")
    address = form.get("address", "")
    city = form.get("city", "")
    zipcode = form.get("zipcode", "")
    business_idea = form.get("business_idea", "")
    desired_name = form.get("desired_name", "")
    business_purpose = form.get("business_purpose", "")
    target_customer = form.get("target_customer", "")
    industry_code = form.get("industry_code", "0")
    duration = form.get("duration", "Perpetual")
    sig_first = form.get("sig_first", "")
    sig_middle = form.get("sig_middle", "")
    sig_last = form.get("sig_last", "")

    # Build names
    full_name = f"{first_name} {middle_name} {last_name}".replace("  ", " ").strip()
    primary_sig = f"{sig_first} {sig_middle} {sig_last}".replace("  ", " ").strip()
    principal_address = f"{address}, {city}, VA {zipcode}"

    # Collect additional member signatures
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
    all_members_str = ", ".join(all_signatures)

    # Business name
    business_name = desired_name.strip() if desired_name.strip() else f"{last_name} Ventures LLC"

    # Run all agents
    name_result = screen_business_name(business_idea)

    llc_result = generate_llc_paperwork(
        business_name=business_name,
        owner_name=full_name,
        business_purpose=business_purpose,
        registered_agent=full_name,
        principal_address=principal_address
    )

    ein_result = generate_ein_guidance(
        business_name=business_name,
        owner_name=full_name,
        state="Virginia"
    )

    brand_result = generate_brand_kit(
        business_name=business_name,
        business_idea=business_idea,
        target_customer=target_customer
    )

    marketing_result = generate_marketing_plan(
        business_name=business_name,
        business_idea=business_idea,
        state="Virginia",
        target_customer=target_customer
    )

    # Generate PDF
    pdf_path = generate_llc_pdf(
        business_name=business_name,
        owner_name=full_name,
        business_purpose=business_purpose,
        registered_agent=full_name,
        principal_address=principal_address,
        signature=primary_sig
    )

    safe_name = business_name.replace(" ", "_").replace("/", "_")
    pdf_filename = f"{safe_name}_LLC_Package.pdf"

    return templates.TemplateResponse(request, "launch_result.html", {
        "full_name": full_name,
        "email": email,
        "business_name": business_name,
        "principal_address": principal_address,
        "industry_code": industry_code,
        "duration": duration,
        "all_signatures": all_signatures,
        "all_members_str": all_members_str,
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
