import re

from playwright.sync_api import sync_playwright
from app.secrets import get_secret
from app.config import SCC_FILING_CARD

CDP_URL = "http://172.27.176.1:9222"
SCC_USERNAME = get_secret("SCC_USERNAME")
SCC_PASSWORD = get_secret("SCC_PASSWORD")

def js_fill_field(page, selector, value):
    """Set a field's value via JS injection, bypassing Playwright's pointer-actionability
    checks. Needed inside the Create Registered Agent modal, where overlapping/animating
    elements in the dialog intercept normal clicks."""
    try:
        page.eval_on_selector(selector, """
            (el, value) => {
                el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('keyup', { bubbles: true }));
            }
        """, value)
        page.wait_for_timeout(300)
        return True
    except Exception as e:
        print(f"⚠️ Could not JS-fill {selector}: {e}")
        return False

def js_check(page, selector):
    try:
        page.eval_on_selector(selector, """
            el => {
                if (!el.checked) {
                    el.checked = true;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('click', { bubbles: true }));
                }
            }
        """)
        page.wait_for_timeout(300)
        return True
    except Exception as e:
        print(f"⚠️ Could not JS-check {selector}: {e}")
        return False

def js_click(page, selector):
    try:
        page.eval_on_selector(selector, "el => el.click()")
        page.wait_for_timeout(300)
        return True
    except Exception as e:
        print(f"⚠️ Could not JS-click {selector}: {e}")
        return False

def resolve_address_verification(page, checkbox_selector, resave_selector=None):
    """SCC pops an 'Address Verification' dialog when it can't auto-match an
    entered address. If it offers a standardized suggestion, check 'Use this
    address' (and click resave_selector again, if given - the Registered
    Agent modal needs a second Done click, the page-level address sections
    don't). Returns False if there's no suggested match (Verified Address:
    N/A), meaning the address genuinely can't be verified online."""
    page.wait_for_timeout(1000)
    if "could not be verified" not in page.inner_text("body").lower():
        return True
    if page.locator(checkbox_selector).count() == 0:
        return False
    js_click(page, checkbox_selector)
    page.wait_for_timeout(1000)
    if resave_selector:
        js_click(page, resave_selector)
        page.wait_for_timeout(2000)
    return "could not be verified" not in page.inner_text("body").lower()

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

def pay_scc_filing_fee(page):
    """Pays the $100 VA SCC state filing fee from the Review page, using
    Launch Bridge's own card (SCC_FILING_CARD in Secret Manager) to front
    the state fee for every customer's filing. Walks the same Shopping
    Cart -> Checkout -> LexisNexis-hosted payment flow a human would click
    through manually, ending in a real, irreversible charge - only called
    when interactive=False (the automated production path)."""
    card = SCC_FILING_CARD
    if not card:
        print("❌ No filing card configured (SCC_FILING_CARD secret empty) - bailing out.")
        page.screenshot(path="/tmp/scc_payment_no_card.png")
        return False

    print("📌 Adding to Shopping Cart...")
    page.click('text=Add To Shopping Cart')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    print("📌 Proceeding to Checkout...")
    page.click('text=Checkout')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    print("📌 Going to Payment...")
    page.click('text=Go To Payment')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    # SCC shows an "I acknowledge..." confirmation dialog before redirecting
    # to the LexisNexis-hosted payment page.
    try:
        page.click('text=I Agree', timeout=5000)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)
    except Exception:
        pass

    print("📌 Filling payment billing info...")
    fill_field(page, '#firstName', card['first_name'])
    fill_field(page, '#lastName', card['last_name'])
    fill_field(page, '#zipcode', card['zip'])
    fill_field(page, '#Address_Line_1', card['address_line1'])
    fill_field(page, '#city', card['city'])
    select_field(page, '#state', card['state'])
    fill_field(page, '#email', card['email'])
    fill_field(page, '#confEmail', card['email'])
    fill_field(page, '#billPhone', card['phone'])

    print("📌 Filling card details...")
    fill_field(page, '#creditCardNumber', card['card_number'])
    select_field(page, '#cardExpMonth', card['exp_month'])
    select_field(page, '#cardExpYear', card['exp_year'])
    fill_field(page, '#cardValidationCode', card['cvc'])

    page.wait_for_timeout(500)
    page.click('#continueButton')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)

    body = page.inner_text("body").lower()
    if "is required" in body or "declined" in body or "invalid" in body:
        print("❌ Payment billing info was rejected - check SCC_FILING_CARD fields.")
        page.screenshot(path="/tmp/scc_payment_billing_error.png")
        return False

    # Custom-styled checkbox - the underlying native input is hidden, so a
    # normal Playwright click can't reach it (see js_check).
    print("📌 Confirming payment authorization...")
    js_check(page, '#perCCAck_input')
    page.wait_for_timeout(500)
    page.click('#continueButton')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(4000)

    body = page.inner_text("body").lower()
    if "declined" in body:
        print("❌ Card was declined by the payment processor.")
        page.screenshot(path="/tmp/scc_payment_declined.png")
        return False

    page.screenshot(path="/tmp/scc_payment_complete.png")
    print("✅ Payment submitted - LLC filing fee paid!")
    return True

class NameTakenError(Exception):
    """Raised when a business name is confirmed taken on Virginia SCC -
    either by verify_name_before_filing's pre-check or by the filing
    wizard's own Step 3 distinguishability check further down in
    file_llc_on_scc. Deliberately a distinct exception type from a plain
    return False, so the caller (run_scc_filing) can tell "this name needs
    to be replaced by the customer" apart from "the filer crashed/hit an
    unrelated site problem" and react differently (notify the customer to
    pick a new name, not just log a generic filing_error for retry)."""
    pass


def verify_name_before_filing(business_name: str) -> dict:
    """Fast pre-check against SCC's public Entity Search, using the local,
    already-logged-in Chrome instance over CDP - a real browser driving
    real DOM interactions, so Virginia SCC's cookie-consent gate and
    reCAPTCHA v3 (both added to that page after app/agents/
    scc_name_check.py's plain-HTTP scraper was written - see that
    module's check_name_public docstring) aren't the hard blockers they
    are for Cloud Run's plain HTTP client.

    Returns {"available": True|False|None, "message": str}. None means
    "couldn't determine either way" (timeout, unexpected page layout,
    no CDP access) - never guess in that case, let the caller fall back to
    a human decision. This is a best-effort speed check meant to catch an
    obviously-taken name before spending several minutes filling out the
    rest of the filing wizard; that wizard's own Step 3 name-
    distinguishability check (further down in file_llc_on_scc) is still
    the real, final, authoritative gate before anything is actually
    submitted."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0]
            page = context.new_page()

            page.goto("https://cis.scc.virginia.gov/EntitySearch/Index")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1000)

            # Dismiss the cookie-consent modal if it's showing - a real
            # click through the page's own "Accept" button
            # (id="acceptCookies"), not the plain-HTTP StoreCookieConsent
            # workaround scc_name_check.py needs.
            if page.locator('#acceptCookies').count() > 0:
                js_click(page, '#acceptCookies')
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1000)

            page.wait_for_selector('#BusinessSearch_Index_txtBusinessName', timeout=15000)
            js_fill_field(page, '#BusinessSearch_Index_txtBusinessName', business_name)

            if page.locator('#BEFilingSearch_ddlSearchLogic').count() > 0:
                try:
                    page.select_option('#BEFilingSearch_ddlSearchLogic', label='Exact Match')
                except Exception:
                    pass  # dropdown may not offer this exact label - fine, default search logic still runs

            page.click('#btnSearch')
            # The search itself is reCAPTCHA v3-gated client-side and
            # reloads the page with results once Google's check clears -
            # give it real time to settle rather than assuming an instant
            # response.
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(2000)

            body = page.inner_text("body")
            page.close()

            # "No records found with your search criteria." is SCC's own
            # exact empty-state text (confirmed by live testing) - the
            # only reliable "definitely available" signal. Do NOT also
            # check for something like "0 records": every results page,
            # match or no match, carries a static footer ("Export
            # limitation for Word is 5000 records...") that trivially
            # contains the substring "0 records" - that false-positive
            # was live-tested and caught before this ever shipped.
            if "no records found" in body.lower():
                return {"available": True, "message": f'"{business_name}" appears to be available on Virginia SCC.'}

            # SCC's stored entity names use a different punctuation
            # convention than what customers type (e.g. "SMITH
            # CONSULTING, L.L.C." vs "Smith Consulting LLC") - strip
            # commas/periods from both sides before comparing so real
            # matches aren't missed on formatting alone (confirmed
            # against a live, real registered entity during testing).
            def _normalize(s: str) -> str:
                return re.sub(r"[.,]", "", s).upper()

            if _normalize(business_name) in _normalize(body):
                return {"available": False, "message": f'"{business_name}" already exists on Virginia SCC.'}

            # Results came back but nothing matched the name exactly -
            # ambiguous rather than a clean yes/no, don't guess.
            return {"available": None, "message": "Could not confirm an exact match either way - please verify manually."}
    except Exception as e:
        print(f"⚠️ verify_name_before_filing crashed for '{business_name}': {e}")
        return {"available": None, "message": f"Could not verify: {e}"}


def file_llc_on_scc(customer_data: dict, interactive=True):
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
    registered_agent_choice = customer_data.get("registered_agent_choice", "self")

    if not business_name.upper().endswith(" LLC"):
        business_name = business_name + " LLC"

    # Safety-net pre-check before touching the login/filing wizard at all -
    # the wizard's own Step 3 name-distinguishability check further below
    # is still the real, final gate, but there's no reason to spend several
    # minutes filling out Entity Information/Registered Agent/Principal
    # Address for a name that's already known to be taken. available=None
    # (couldn't verify) is not a rejection - proceeds to the wizard's own
    # check same as always, per verify_name_before_filing's docstring.
    pre_check = verify_name_before_filing(business_name)
    print(f"📋 Pre-filing name check for '{business_name}': {pre_check}")
    if pre_check["available"] is False:
        raise NameTakenError(pre_check["message"])

    ra_label = "Leo Fall (Launch Bridge)" if registered_agent_choice == "launchbridge" else "Self (customer)"
    print(f"\n🚀 Filing LLC for: {business_name}")
    print(f"📋 Registered Agent: {ra_label}")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.new_page()

        # Log in with real browser (CDP-connected, not a fresh automated
        # Chromium) so SCC's bot detection doesn't block the login attempt.
        print("📌 Logging into SCC...")
        page.goto("https://cis.scc.virginia.gov/Account/Login")
        page.wait_for_load_state("networkidle")
        if page.locator('#txtUsername').count() > 0:
            page.fill('#txtUsername', SCC_USERNAME)
            page.fill('#txtPassword', SCC_PASSWORD)
            page.click('#Login')
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)

        if "Hi," not in page.inner_text("body"):
            print("❌ Login failed - check SCC_USERNAME/SCC_PASSWORD in .env")
            page.screenshot(path="/tmp/scc_login_failed.png")
            browser.close()
            return False
        print("✅ Logged into SCC")

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
            if not interactive:
                print("❌ Could not determine name availability - bailing out (non-interactive mode)")
                page.screenshot(path="/tmp/scc_name_check_unclear.png")
                browser.close()
                return False
            answer = input("Is name available? (y/n): ")
            if answer.lower() != 'y':
                browser.close()
                return False

        page.wait_for_timeout(2000)
        body = page.inner_text("body")
        if "is not distinguishable" in body or "Name Conflict" in body:
            print(f"❌ Name NOT available")
            if interactive:
                input("Press ENTER to close...")
                browser.close()
                return False
            # Confirmed rejection (not just an unclear/timeout result, see
            # the except block above for that case) - same treatment as
            # verify_name_before_filing's pre-check: a distinct,
            # non-retryable outcome the caller needs to notify the
            # customer about, not a generic filing crash.
            browser.close()
            raise NameTakenError(f'"{business_name}" is not distinguishable from an existing Virginia entity.')

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

        if registered_agent_choice == "self":
            # PATH B: customer is their own RA. Click Create Individual RA -
            # opens a jQuery UI dialog (#dialog).
            print("📌 Clicking Create Individual RA...")
            page.wait_for_selector('#createAgent', timeout=15000)
            page.locator('#createAgent').scroll_into_view_if_needed()
            page.click('#createAgent')
            page.wait_for_selector('#dialog', state='visible', timeout=10000)
            page.wait_for_timeout(1500)

            page.screenshot(path="/tmp/scc_ra_modal.png")
            print("📸 RA screenshot saved")

            # The dialog's fields live under createAgentinfo_NameFields_* and
            # documentProcessingStep_CreateStatutoryAgentModal_PrincipalAddess_*
            # (NOT the NewLLCFormation_*/NewRAStatAgentPrincipalAddress_* ids,
            # which are a different, hidden form elsewhere on the page that
            # happens to sit at the same screen coordinates). Elements inside
            # the dialog intercept normal Playwright clicks while it settles/
            # repositions, so fields are set via JS injection instead of
            # click+fill.
            print("📌 Filling RA modal fields via JS injection...")
            js_fill_field(page, '#createAgentinfo_NameFields_RegisterAgentTypeId', '8')  # Member or Manager of the LLC
            js_fill_field(page, '#createAgentinfo_NameFields_FirstName', first_name)
            if middle_name:
                js_fill_field(page, '#createAgentinfo_NameFields_MiddleName', middle_name)
            js_fill_field(page, '#createAgentinfo_NameFields_LastName', last_name)
            js_check(page, '#createAgentinfo_NameFields_ResidentOfVA')

            js_fill_field(page, '#documentProcessingStep_CreateStatutoryAgentModal_PrincipalAddess_StreetAddress1', street)
            js_fill_field(page, '#documentProcessingStep_CreateStatutoryAgentModal_PrincipalAddess_Zip5', zipcode)
            page.wait_for_timeout(2000)
            js_fill_field(page, '#documentProcessingStep_CreateStatutoryAgentModal_PrincipalAddess_CityDD', city)
            js_fill_field(page, '#documentProcessingStep_CreateStatutoryAgentModal_PrincipalAddess_State', 'VA')
            js_fill_field(page, '#documentProcessingStep_CreateStatutoryAgentModal_PrincipalAddess_EmailAddress', email)

            # Save RA
            print("📌 Saving RA...")
            js_click(page, '#btnCreateAgent')
            page.wait_for_timeout(2500)

            if "could not be verified" in page.inner_text("body").lower():
                print("⚠️  Address Verification dialog appeared - checking 'Use this address' and resaving...")
                resolved = resolve_address_verification(
                    page,
                    '#documentProcessingStep_CreateStatutoryAgentModal_PrincipalAddessVerifiedAddress',
                    resave_selector='#btnCreateAgent'
                )
                if resolved:
                    print("✅ Address verified, RA saved")
                else:
                    # No suggested match was offered (Verified Address: N/A) - SCC
                    # requires a paper filing for addresses it truly can't match.
                    print("❌ Address has no suggested match - SCC cannot verify it online.")
                    print("📸 Saving screenshot - review and correct the address manually in the browser.")
                    page.screenshot(path="/tmp/scc_ra_address_unverified.png")
                    if not interactive:
                        print("Bailing out (non-interactive mode) - this address needs manual correction.")
                        browser.close()
                        return False
                    print("Press ENTER once resolved in the browser to continue...")
                    input()
        else:
            # PATH A (Leo Fall as RA) available but hidden from UI pending RA partner setup.
            # The search-and-select flow below is fully implemented and tested.
            # already a registered SCC agent, so this is a search-and-select
            # against the existing agent search rather than the Create
            # Individual RA flow - selecting from search results sets the
            # RA's address from SCC's own records, so there's no address
            # entry or address-verification step on this path.
            print("📌 Searching for existing RA (Leo Fall)...")
            page.wait_for_selector('#rdnNewLLCFormation_Individual', timeout=15000)
            js_check(page, '#rdnNewLLCFormation_Individual')
            fill_field(page, '#NewLLCFormation_FirstName', "Leo")
            fill_field(page, '#NewLLCFormation_LastName', "Fall")
            page.wait_for_timeout(300)
            page.locator('#btnSearchAgent').scroll_into_view_if_needed()
            page.click('#btnSearchAgent')
            page.wait_for_selector('input[name="SelectedAgentID"]', timeout=15000)
            page.wait_for_timeout(1000)

            page.screenshot(path="/tmp/scc_ra_search_results.png")
            print("📸 RA search results screenshot saved")

            print("📌 Selecting Leo Fall from search results...")
            page.click('input[name="SelectedAgentID"]')
            page.wait_for_timeout(2000)

            # Selecting an existing agent from search reveals an "Update
            # Registered Agent Information" panel with two more required
            # fields SCC won't let you past without: RA Capacity (his
            # relationship to the LLC - "Member or Manager of the LLC",
            # same value Path B's Create Individual RA modal uses) and the
            # "resident of Virginia" checkbox. Skipping these is exactly
            # what was causing the filing to stall out here.
            print("📌 Setting RA Capacity and Virginia residency...")
            page.wait_for_selector('#NewRAStatAgentName_RegisterAgentTypeId', timeout=15000)
            select_field(page, '#NewRAStatAgentName_RegisterAgentTypeId', '8')
            js_check(page, '#NewRAStatAgentName_ResidentOfVA')

        # === PRINCIPAL OFFICE ADDRESS ===
        print("📌 Going to Principal Office Address...")
        click_next(page)
        page.wait_for_timeout(3000)

        fill_field(page, '#PrincipalOfficeAddress_StreetAddress1', street)
        fill_field(page, '#PrincipalOfficeAddress_Zip5', zipcode)
        page.wait_for_timeout(2000)
        fill_field(page, '#PrincipalOfficeAddress_CityDD', city)
        select_field(page, '#PrincipalOfficeAddress_State', 'VA')
        # Note: this section has no Email Address field on the visible page -
        # unlike the RA modal, there's no #PrincipalOfficeAddress_EmailAddress
        # to fill here.

        # === PRINCIPAL INFORMATION ===
        # The Address Verification dialog (if any) is triggered by this
        # click, not by filling the fields above.
        print("📌 Going to Principal Information...")
        click_next(page)

        if "could not be verified" in page.inner_text("body").lower():
            print("⚠️  Address Verification dialog appeared - checking 'Use this address'...")
            resolved = resolve_address_verification(page, '#PrincipalOfficeAddressVerifiedAddress')
            if resolved:
                print("✅ Principal Office Address verified")
            else:
                print("❌ Address has no suggested match - SCC cannot verify it online.")
                print("📸 Saving screenshot - review and correct the address manually in the browser.")
                page.screenshot(path="/tmp/scc_principal_address_unverified.png")
                if not interactive:
                    print("Bailing out (non-interactive mode) - this address needs manual correction.")
                    browser.close()
                    return False
                print("Press ENTER once resolved in the browser to continue...")
                input()
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
        # The Signature field requires the signer's full typed name - it's
        # separate from (and in addition to) the Printed Name first/last
        # fields below. Title is pre-filled and disabled, so it's skipped.
        full_signature_name = f"{first_name} {middle_name} {last_name}".replace("  ", " ").strip()
        fill_field(page, '#NewLLCFormation_CurrentSignature_Name', full_signature_name)
        fill_field(page, '#NewLLCFormation_CurrentSignature_PrintedName_FirstName', first_name)
        if middle_name:
            fill_field(page, '#NewLLCFormation_CurrentSignature_PrintedName_MiddleName', middle_name)
        fill_field(page, '#NewLLCFormation_CurrentSignature_PrintedName_LastName', last_name)
        page.locator('#AddSignatureBtn').scroll_into_view_if_needed()
        page.click('#AddSignatureBtn')
        page.wait_for_timeout(1000)

        # Dismiss the "Signature Added Successfully" confirmation dialog
        try:
            page.click('button:has-text("OK")', timeout=5000)
            page.wait_for_timeout(500)
        except Exception:
            pass

        # === REVIEW ===
        print("📌 Going to Review...")
        click_next(page)

        page.screenshot(path="/tmp/scc_filled.png")
        print("\n✅ All steps filled - on Review page!")
        print("📸 Screenshot saved")

        if not interactive:
            print("📌 Proceeding to payment (non-interactive mode)...")
            paid = pay_scc_filing_fee(page)
            browser.close()
            return paid

        print("\n⚠️  REVIEW EVERYTHING IN THE BROWSER")
        print("Click 'Add To Shopping Cart' to proceed and pay $100 with your card")
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
        "duration": "Perpetual",
        "registered_agent_choice": "launchbridge"
    }
    file_llc_on_scc(test_customer)
