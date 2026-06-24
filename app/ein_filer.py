import re

from playwright.sync_api import sync_playwright

CDP_URL = "http://172.27.176.1:9222"
EIN_PATTERN = re.compile(r"\b\d{2}-\d{7}\b")

def fill_field(page, selector, value):
    try:
        field = page.locator(selector).first
        field.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        field.click()
        page.wait_for_timeout(200)
        field.fill(value)
        page.wait_for_timeout(300)
        return True
    except Exception as e:
        print(f"⚠️ Could not fill {selector}: {e}")
        return False

def type_field(page, selector, value):
    """Some IRS fields run JS keystroke validation that ignores Playwright's
    .fill() (which sets the value via a single synthetic event) and reverts
    to empty on blur. Typing character-by-character fires real key events
    the page's handler actually processes."""
    try:
        field = page.locator(selector).first
        field.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        field.click()
        page.wait_for_timeout(200)
        field.press("Control+A")
        field.press("Delete")
        field.type(value, delay=30)
        page.wait_for_timeout(300)
        return True
    except Exception as e:
        print(f"⚠️ Could not type into {selector}: {e}")
        return False

def select_field(page, selector, value=None, label=None):
    try:
        field = page.locator(selector).first
        field.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        if label:
            page.select_option(selector, label=label)
        else:
            page.select_option(selector, value=value)
        page.wait_for_timeout(300)
        return True
    except Exception as e:
        print(f"⚠️ Could not select {selector}: {e}")
        return False

def click_radio(page, name, value):
    try:
        input_el = page.locator(f'input[name="{name}"][value="{value}"]').first
        input_id = input_el.get_attribute("id")
        page.locator(f'label[for="{input_id}"]').click()
        page.wait_for_timeout(300)
        return True
    except Exception as e:
        print(f"⚠️ Could not click radio {name}={value}: {e}")
        return False

def click_continue(page):
    page.locator('a:has-text("Continue")').last.click()
    page.wait_for_load_state("load")
    page.wait_for_timeout(2000)
    print(f"  → Step complete ({page.url})")

def _build_fallback_confirmation_pdf(business_name: str, ein: str, body_text: str, screenshot_path: str) -> bytes:
    """A guaranteed-to-work record of the EIN confirmation when the IRS
    site's own downloadable letter can't be captured (link text changed,
    it's a JS-triggered download instead of a plain href, etc) - built
    from the screenshot/text this function already had on hand anyway,
    so there's always something real and useful in the dashboard's
    documents instead of silently nothing (the gap that left Govcon Ramp
    LLC's EIN letter missing entirely - see _capture_cp575 below)."""
    import io
    import os
    from reportlab.lib.pagesizes import letter as pagesize_letter
    from reportlab.lib.units import inch
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=pagesize_letter, topMargin=inch, bottomMargin=inch)
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"EIN Confirmation Record - {business_name}", styles["Title"]),
        Spacer(1, 0.15 * inch),
    ]
    if ein:
        story.append(Paragraph(f"EIN: {ein}", styles["Heading2"]))
        story.append(Spacer(1, 0.2 * inch))
    # reportlab's Image flowable opens the file lazily at build() time,
    # not at construction - checking existence up front is the only way
    # to actually skip a missing screenshot instead of crashing build().
    if os.path.exists(screenshot_path):
        story.append(Image(screenshot_path, width=6.5 * inch, height=6.5 * inch, kind="proportional"))
        story.append(Spacer(1, 0.2 * inch))
    for line in body_text.splitlines():
        line = line.strip()
        if line:
            story.append(Paragraph(line, styles["Normal"]))
    doc.build(story)
    return buffer.getvalue()

def _capture_cp575(page, context, business_name: str, ein: str) -> bytes | None:
    """Best-effort capture of the IRS's own CP575 confirmation letter,
    falling back to a self-built PDF record (see
    _build_fallback_confirmation_pdf) if that fails for any reason -
    this should essentially never return None, so a captured EIN never
    again ends up with no document in the dashboard at all."""
    body_text = page.inner_text("body")

    try:
        for link_text in ("EIN Confirmation Letter", "Confirmation Letter", "CP575", "CP 575", "Notice", "Download"):
            link = page.locator(f'a:has-text("{link_text}")').first
            if link.count() == 0:
                continue

            href = link.get_attribute("href")
            if href:
                try:
                    resp = context.request.get(href)
                    if resp.ok and resp.body().startswith(b"%PDF"):
                        return resp.body()
                except Exception:
                    pass

            # Some IRS pages trigger the letter via a JS-driven download
            # rather than a plain fetchable href - clicking and capturing
            # the resulting download event covers that case too.
            try:
                with page.expect_download(timeout=10000) as download_info:
                    link.click()
                with open(download_info.value.path(), "rb") as f:
                    candidate = f.read()
                if candidate.startswith(b"%PDF"):
                    return candidate
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ Could not download the IRS's own CP575 confirmation letter (non-fatal): {e}")

    print("⚠️ Falling back to a self-built confirmation PDF instead of the IRS's own CP575.")
    try:
        return _build_fallback_confirmation_pdf(business_name, ein, body_text, "/tmp/ein_confirmation.png")
    except Exception as e:
        print(f"⚠️ Could not build a fallback confirmation PDF either (non-fatal): {e}")
        return None

def file_ein_with_irs(customer_data: dict, interactive=True, on_submitted=None):
    """Returns a result dict, never a bare bool:
      {"success": True, "ein": "XX-XXXXXXX", "cp575_bytes": bytes|None}
        - the IRS issued an EIN and it was read back successfully.
      {"success": False, "rejected": True}
        - IRS rejected the application before any submission happened
          (e.g. SSN didn't match IRS records) - safe to fix and retry.
      {"success": False, "submitted": True, "ein": None, "screenshot": path}
        - Submit was clicked (a real, permanent EIN now exists at the
          IRS) but the confirmation page's EIN couldn't be read back.
          Callers MUST NOT retry filing for this order - that would file
          a second, duplicate EIN - and must recover the number manually
          (the screenshot, or the IRS site/mail) instead.
      {"success": False, "submitted": False, "error": str}
        - Some other failure before Submit was ever reached. Safe to retry.

    on_submitted, if given, is called with no arguments the instant the
    Submit click (or, in interactive mode, the human's manual submission)
    is confirmed to have happened - independent of whatever succeeds or
    crashes afterward, so a caller can durably record "this is now a real,
    permanent IRS filing" before attempting anything as fallible as
    reading the confirmation page back."""
    business_name = customer_data["business_name"]
    if not business_name.upper().endswith(" LLC"):
        business_name = business_name + " LLC"

    first_name = customer_data["first_name"]
    middle_name = customer_data.get("middle_name", "")
    last_name = customer_data["last_name"]
    ssn = customer_data["ssn"]
    street = customer_data["address"]
    city = customer_data["city"]
    state = customer_data.get("state", "VA")
    zipcode = customer_data["zipcode"]
    phone = customer_data["phone"].replace("(", "").replace(")", "").replace("-", "").replace(" ", "")
    county = customer_data["county"]
    start_month = customer_data.get("start_month", "January")
    start_year = customer_data["start_year"]
    members = customer_data.get("members", "1")
    business_description = customer_data.get("business_description", "Business consulting services")

    print(f"\n🚀 Applying for EIN: {business_name}")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.new_page()

        # === LANDING ===
        print("📌 Opening IRS EIN Assistant...")
        page.goto("https://sa.www4.irs.gov/applyein/", wait_until="load")
        page.wait_for_timeout(1500)
        page.click('#anchor-ui-0')
        page.wait_for_load_state("load")
        page.wait_for_timeout(1500)

        # === STEP 1: LEGAL STRUCTURE ===
        print("📌 Step 1: Legal Structure...")
        click_radio(page, "legalStructureInput", "LLC")
        page.wait_for_timeout(800)

        fill_field(page, "#membersOfLlcInput", str(members))
        select_field(page, "#stateInputControl", value=state)
        page.wait_for_timeout(800)

        click_radio(page, "reasonForApplyingInputControl", "NEW_BUSINESS")
        page.wait_for_timeout(500)
        click_continue(page)

        # === STEP 2: IDENTITY ===
        print("📌 Step 2: Identity (Responsible Party)...")
        fill_field(page, "#responsibleSsn", ssn)
        fill_field(page, "#responsibleFirstName", first_name)
        if middle_name:
            fill_field(page, "#responsibleMiddleName", middle_name)
        fill_field(page, "#responsibleLastName", last_name)
        click_radio(page, "entityRoleRadioInput", "yes")
        click_continue(page)

        if "We are unable to provide you with an EIN" in page.inner_text("body"):
            print("❌ IRS rejected the application at the Identity step (SSN did not match IRS records).")
            print("📸 Saving screenshot for review")
            page.screenshot(path="/tmp/ein_rejected.png")
            return {"success": False, "rejected": True}

        # === STEP 3: ADDRESSES ===
        print("📌 Step 3: Addresses...")
        fill_field(page, "#physicalStreet", street)
        fill_field(page, "#physicalCity", city)
        select_field(page, "#physicalState", value=state)
        fill_field(page, "#physicalZipCode", zipcode)
        fill_field(page, "#thePhone", phone)
        click_radio(page, "otherAddress", "no")
        click_continue(page)

        # === STEP 4: ADDITIONAL DETAILS (LLC info) ===
        print("📌 Step 4: Additional Details...")
        fill_field(page, "#legalNameInput", business_name)
        fill_field(page, "#countyInput", county)
        select_field(page, "#stateInput", value=state)
        select_field(page, "#StateFiledArticlesOrganizationInput", value=state)
        select_field(page, "#startDateMonthInput", label=start_month)
        fill_field(page, "#startDateYearInput", str(start_year))

        click_radio(page, "highwayVehiclesInput", "no")
        click_radio(page, "gamblingWagerInput", "no")
        click_radio(page, "fileForm720Input", "no")
        click_radio(page, "atfInput", "no")
        click_radio(page, "hasEmployeesInput", "no")
        click_continue(page)

        # === STEP 4b: BUSINESS ACTIVITY ===
        print("📌 Step 4b: Business Activity...")
        click_radio(page, "entityBusinessCategoryInput", "OTHER")
        page.wait_for_selector('input[name="otherInput"]', state="visible", timeout=10000)
        click_radio(page, "otherInput", "OTHER")
        page.wait_for_selector("#otherActivityTextInput", state="visible", timeout=10000)

        for attempt in range(3):
            type_field(page, "#otherActivityTextInput", business_description)
            actual_value = page.eval_on_selector("#otherActivityTextInput", "el => el.value")
            if actual_value.strip():
                break
            print(f"⚠️ Other Activity text field empty after type attempt {attempt + 1}, retrying...")
            page.wait_for_timeout(500)

        click_continue(page)

        if "is now required based on previous selection" in page.inner_text("body").lower():
            print("⚠️ Business Activity validation error after Continue - retrying...")
            page.wait_for_selector("#otherActivityTextInput", state="visible", timeout=10000)
            type_field(page, "#otherActivityTextInput", business_description)
            click_continue(page)

        # === STEP 5: REVIEW & SUBMIT ===
        print("📌 Step 5: Review & Submit...")
        click_radio(page, "confirmationLetterRadioInput", "DIGITAL")
        page.wait_for_timeout(500)

        page.screenshot(path="/tmp/ein_review.png", full_page=True)
        print("\n✅ All steps filled!")
        print("📸 Screenshot saved as /tmp/ein_review.png")

        if interactive:
            print("\n⚠️  REVIEW EVERYTHING IN THE BROWSER BEFORE SUBMITTING")
            print("Submitting issues a real, permanent EIN immediately — this cannot be undone.")
            print("Click 'Submit EIN Request' yourself in the browser, then press ENTER here.")
            input()
        else:
            print("⚠️ Auto-submitting - this issues a real, permanent EIN immediately.")
            submit_button = None
            for text in ("Submit EIN Request", "Submit Request", "Submit"):
                candidate = page.locator(f'button:has-text("{text}"), a:has-text("{text}")').last
                if candidate.count() > 0:
                    submit_button = candidate
                    break
            if submit_button is None:
                print("❌ Could not find a Submit button on the Review page.")
                page.screenshot(path="/tmp/ein_confirmation.png", full_page=True)
                return {"success": False, "submitted": False, "error": "Could not find Submit button on Review page"}
            submit_button.click()
            page.wait_for_load_state("load", timeout=30000)
            page.wait_for_timeout(2000)

        # The Submit click (or the human's manual one, above) has now
        # definitely happened - a real EIN exists at the IRS regardless of
        # what happens from here on. Record that durably before doing
        # anything else, so a crash mid-scrape can never look like "filing
        # never happened" to a caller deciding whether it's safe to retry.
        if on_submitted:
            on_submitted()

        # === CONFIRMATION: read back the issued EIN ===
        print("📌 Reading the confirmation page for the issued EIN...")
        page.wait_for_timeout(1000)
        page.screenshot(path="/tmp/ein_confirmation.png", full_page=True)
        body_text = page.inner_text("body")
        match = EIN_PATTERN.search(body_text)

        cp575_bytes = _capture_cp575(page, context, business_name, match.group(0) if match else "")

        if match:
            ein = match.group(0)
            print(f"✅ EIN issued: {ein}")
            return {"success": True, "ein": ein, "cp575_bytes": cp575_bytes}

        print("⚠️ Submitted, but could not find an EIN on the confirmation page.")
        print("📸 Screenshot saved to /tmp/ein_confirmation.png for manual review.")
        return {"success": False, "submitted": True, "ein": None, "screenshot": "/tmp/ein_confirmation.png", "cp575_bytes": cp575_bytes}

if __name__ == "__main__":
    test_customer = {
        "business_name": "Stellar Launch Bridge Ventures",
        "first_name": "John",
        "last_name": "Smith",
        "ssn": "123-45-6789",
        "address": "123 Main Street",
        "city": "Arlington",
        "state": "VA",
        "zipcode": "22201",
        "phone": "7035550100",
        "county": "Arlington",
        "start_month": "June",
        "start_year": "2026",
        "members": "1",
        "business_description": "Business consulting services",
    }
    result = file_ein_with_irs(test_customer)
    print(result)
