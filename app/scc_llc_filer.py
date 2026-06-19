import os
import json
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

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

def select_field(page, selector, value):
    try:
        field = page.locator(selector).first
        field.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        page.select_option(selector, value=value)
        page.wait_for_timeout(300)
        return True
    except Exception as e:
        print(f"⚠️ Could not select {selector}: {e}")
        return False

def click_next(page):
    page.locator('#btnNext').scroll_into_view_if_needed()
    page.click('#btnNext')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(5000)
    print(f"  → Step complete")

def file_llc_on_scc(customer_data: dict):
    business_name = customer_data["business_name"]
    first_name = customer_data["first_name"]
    middle_name = customer_data.get("middle_name", "")
    last_name = customer_data["last_name"]
    email = customer_data["email"]
    phone = customer_data["phone"].replace("(","").replace(")","").replace("-","").replace(" ","")
    street = customer_data["address"]
    city = customer_data["city"]
    zipcode = customer_data["zipcode"]
    industry_code = customer_data.get("industry_code", "0")

    if not business_name.upper().endswith(" LLC"):
        business_name = business_name + " LLC"

    print(f"\n🚀 Filing LLC for: {business_name}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=500)
        context = browser.new_context()

        with open("scc_session.json", "r") as f:
            cookies = json.load(f)
        context.add_cookies(cookies)
        print("✅ Loaded saved session")

        page = context.new_page()

        # Verify session
        page.goto("https://cis.scc.virginia.gov/OnlineDashboard/Index")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)
        if "Login" in page.title():
            print("❌ Session expired - run save_scc_session.py first")
            browser.close()
            return False
        print("✅ Session valid")

        # Step 1
        print("📌 Step 1: Virginia Entity...")
        page.goto("https://cis.scc.virginia.gov/BusinesEntityOnlineShared/CreateBusiness?categoryID=Form%20or%20Register%20a%20Business")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)
        page.click('#rdDomesticEntity')
        page.wait_for_timeout(500)
        page.click('#btnContinue')
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)

        # Step 2
        print("📌 Step 2: LLC + Articles of Organization...")
        page.select_option('#entityType', label='Limited Liability Company')
        page.wait_for_timeout(1000)
        page.select_option('#filingType', value='25')
        page.wait_for_timeout(1000)
        page.click('#btnContinue')
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(5000)

        # Wait for name reservation radio to appear
        print("📌 Step 3: Waiting for name check page to load...")
        page.wait_for_selector('#nameReservationNo', timeout=30000)
        page.wait_for_timeout(2000)

        # Step 3 - Name check
        page.click('#nameReservationNo')
        page.wait_for_timeout(2000)
        page.fill('#SharedSteps_EntitySearch_EntityName', business_name)
        page.wait_for_timeout(500)
        print(f"⏳ Checking: {business_name}")
        page.click('#btnSearch')

        try:
            page.wait_for_function(
                """() => {
                    const body = document.body.innerText;
                    return body.includes('is distinguishable') || 
                           body.includes('is not distinguishable') ||
                           body.includes('Name Conflict') ||
                           body.includes('Core Name');
                }""",
                timeout=30000
            )
        except:
            answer = input("Is name available? (y/n): ")
            if answer.lower() != 'y':
                browser.close()
                return False

        page.wait_for_timeout(2000)
        body = page.inner_text("body")
        if "is not distinguishable" in body or "Name Conflict" in body:
            print(f"❌ Name NOT available")
            input("Press ENTER to close...")
            browser.close()
            return False

        print(f"✅ Name AVAILABLE!")

        # === ENTITY INFORMATION ===
        print("📌 Going to Entity Information...")
        click_next(page)
        fill_field(page, '#DocumentProcessingSteps_EntityInformation_NewLLCFormation_EntityEmailAddressTextBox', email)
        fill_field(page, '#DocumentProcessingSteps_EntityInformation_NewLLCFormation_EntityContactNumberTextBox', phone)
        select_field(page, '#NewLLCFormation_EntityCharacterOfBusiness_CharacterOfBusinessId', industry_code)
        try:
            radios = page.query_selector_all('input[name="NewLLCFormation.EntityDuration.DurationId"]')
            if radios:
                radios[0].scroll_into_view_if_needed()
                radios[0].click()
            page.wait_for_timeout(300)
        except:
            pass

        # === REGISTERED AGENT ===
        print("📌 Going to Registered Agent...")
        click_next(page)
        page.wait_for_timeout(3000)

        # Click Create Individual RA
        print("📌 Clicking Create Individual RA...")
        page.wait_for_selector('#createAgent', timeout=15000)
        page.locator('#createAgent').scroll_into_view_if_needed()
        page.click('#createAgent')
        page.wait_for_timeout(3000)

        page.screenshot(path="scc_ra_modal.png")
        print("📸 RA screenshot saved")

        # Fill RA details
        fill_field(page, '#NewLLCFormation_FirstName', first_name)
        if middle_name:
            fill_field(page, '#NewLLCFormation_MiddleName', middle_name)
        fill_field(page, '#NewLLCFormation_LastName', last_name)

        try:
            page.locator('input[name="NewLLCFormation_ResidentOfVA"]').first.scroll_into_view_if_needed()
            page.check('input[name="NewLLCFormation_ResidentOfVA"]')
            page.wait_for_timeout(300)
        except Exception as e:
            print(f"⚠️ VA resident checkbox: {e}")

        fill_field(page, '#NewRAStatAgentPrincipalAddress_StreetAddress1', street)
        fill_field(page, '#NewRAStatAgentPrincipalAddress_Zip5', zipcode)
        page.wait_for_timeout(2000)
        fill_field(page, '#NewRAStatAgentPrincipalAddress_CityDD', city)
        select_field(page, '#NewRAStatAgentPrincipalAddress_State', 'VA')
        fill_field(page, '#NewRAStatAgentPrincipalAddress_EmailAddress', email)

        # Save RA
        try:
            save_btns = page.query_selector_all("input[type=button], button")
            for btn in save_btns:
                text = btn.inner_text().strip().lower()
                val = (btn.get_attribute('value') or '').lower()
                if 'save' in text or 'save' in val or 'submit' in text:
                    print(f"📌 Clicking: {text or val}")
                    btn.scroll_into_view_if_needed()
                    btn.click()
                    page.wait_for_timeout(2000)
                    break
        except Exception as e:
            print(f"⚠️ Save button: {e}")

        # === PRINCIPAL OFFICE ADDRESS ===
        print("📌 Going to Principal Office Address...")
        click_next(page)
        page.wait_for_timeout(3000)

        fill_field(page, '#PrincipalOfficeAddress_StreetAddress1', street)
        fill_field(page, '#PrincipalOfficeAddress_Zip5', zipcode)
        page.wait_for_timeout(2000)
        fill_field(page, '#PrincipalOfficeAddress_CityDD', city)
        select_field(page, '#PrincipalOfficeAddress_State', 'VA')
        fill_field(page, '#PrincipalOfficeAddress_EmailAddress', email)

        # === PRINCIPAL INFORMATION ===
        print("📌 Going to Principal Information...")
        click_next(page)
        page.wait_for_timeout(3000)

        # === UPLOAD ATTACHMENTS ===
        print("📌 Going to Upload Attachments...")
        click_next(page)
        page.wait_for_timeout(3000)

        # === SIGNATURES ===
        print("📌 Going to Signatures...")
        click_next(page)
        page.wait_for_timeout(3000)

        page.locator('#Signature_IndividualRadioButton').scroll_into_view_if_needed()
        page.click('#Signature_IndividualRadioButton')
        page.wait_for_timeout(300)
        fill_field(page, '#NewLLCFormation_CurrentSignature_PrintedName_FirstName', first_name)
        if middle_name:
            fill_field(page, '#NewLLCFormation_CurrentSignature_PrintedName_MiddleName', middle_name)
        fill_field(page, '#NewLLCFormation_CurrentSignature_PrintedName_LastName', last_name)
        fill_field(page, '#NewLLCFormation_CurrentSignature_Title', 'Organizer')
        page.locator('#AddSignatureBtn').scroll_into_view_if_needed()
        page.click('#AddSignatureBtn')
        page.wait_for_timeout(1000)

        page.screenshot(path="scc_filled.png")
        print("\n✅ All steps filled!")
        print("📸 Screenshot saved")
        print("\n⚠️  REVIEW EVERYTHING IN THE BROWSER")
        print("Click Next to Review, then Submit and pay $100 with your card")
        print("Press ENTER here when completely done...")
        input()

        browser.close()
        return True

if __name__ == "__main__":
    test_customer = {
        "business_name": "Stellar Launch Bridge Ventures",
        "first_name": "John",
        "middle_name": "",
        "last_name": "Smith",
        "email": "john@example.com",
        "phone": "7035550100",
        "address": "123 Main Street",
        "city": "Arlington",
        "zipcode": "22201",
        "industry_code": "0",
        "duration": "Perpetual"
    }
    file_llc_on_scc(test_customer)
