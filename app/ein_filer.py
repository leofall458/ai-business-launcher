from playwright.sync_api import sync_playwright

CDP_URL = "http://172.27.176.1:9222"

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

def file_ein_with_irs(customer_data: dict, interactive=True):
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
            page.screenshot(path="ein_rejected.png")
            return False

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
        page.wait_for_timeout(800)
        click_radio(page, "otherInput", "OTHER")
        page.wait_for_timeout(800)
        fill_field(page, "#otherActivityTextInput", business_description)
        click_continue(page)

        # === STEP 5: REVIEW & SUBMIT ===
        print("📌 Step 5: Review & Submit...")
        click_radio(page, "confirmationLetterRadioInput", "DIGITAL")
        page.wait_for_timeout(500)

        page.screenshot(path="ein_review.png", full_page=True)
        print("\n✅ All steps filled!")
        print("📸 Screenshot saved as ein_review.png")
        print("\n⚠️  REVIEW EVERYTHING IN THE BROWSER BEFORE SUBMITTING")
        print("Submitting issues a real, permanent EIN immediately — this cannot be undone.")
        print("Click 'Submit EIN Request' yourself in the browser when ready.")
        if not interactive:
            print("(non-interactive mode - leaving the tab open on the Review page for manual submission)")
            return True
        print("Press ENTER here when completely done...")
        input()

        return True

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
    file_ein_with_irs(test_customer)
