from playwright.sync_api import sync_playwright
from app.secrets import get_secret

SCC_USERNAME = get_secret("SCC_USERNAME")
SCC_PASSWORD = get_secret("SCC_PASSWORD")

SCC_NAME_CHECK_URL = "https://cis.scc.virginia.gov/Account/NameCheckAvailability"
SCC_ENTITY_SEARCH_URL = "https://cis.scc.virginia.gov/EntitySearch/Index"

def _connect_to_local_chrome(p):
    """The real SCC checks drive a real, already-logged-in Chrome browser
    over CDP on whatever machine has one open - never reachable from a
    deployed server like Cloud Run. Isolated into its own helper so that
    specific failure (no local browser to drive) can be told apart from a
    genuine SCC-side error once we're actually talking to the site."""
    return p.chromium.connect_over_cdp("http://172.27.176.1:9222")

def check_name_on_scc(business_name: str) -> dict:
    try:
        with sync_playwright() as p:
            try:
                browser = _connect_to_local_chrome(p)
            except Exception:
                return {
                    "available": None,
                    "status": "UNAVAILABLE",
                    "message": "SCC name check is temporarily unavailable.",
                    "link": SCC_NAME_CHECK_URL,
                    "conflicts": [],
                    "raw": "",
                }
            context = browser.contexts[0]
            page = context.new_page()

            # Login
            page.goto("https://cis.scc.virginia.gov/Account/Login")
            page.wait_for_load_state("networkidle")
            page.fill('#txtUsername', SCC_USERNAME)
            page.fill('#txtPassword', SCC_PASSWORD)
            page.click('#Login')
            page.wait_for_load_state("networkidle")

            # Go to name check
            page.goto("https://cis.scc.virginia.gov/Account/NameCheckAvailability")
            page.wait_for_load_state("networkidle")

            # Fill and submit
            page.fill('#txtEntityName', business_name)
            page.click('#Check_Name')

            # Wait for result to load
            page.wait_for_function("!document.body.innerText.includes('Please wait')", timeout=15000)
            page.wait_for_timeout(2000)

            body_text = page.inner_text("body")
            page.close()

            # Parse result
            if "is not distinguishable" in body_text.lower():
                # Extract conflicting names
                conflicts = []
                lines = body_text.split('\n')
                for line in lines:
                    if 'Active' in line or 'Inactive' in line:
                        parts = line.strip().split('\t')
                        if len(parts) >= 2:
                            conflicts.append(parts[1].strip())

                conflict_str = ", ".join(conflicts) if conflicts else "existing Virginia businesses"

                return {
                    "available": False,
                    "status": "TAKEN",
                    "message": f'"{business_name}" is not distinguishable from {conflict_str}.',
                    "conflicts": conflicts,
                    "raw": body_text
                }

            elif "is distinguishable" in body_text.lower():
                return {
                    "available": True,
                    "status": "AVAILABLE",
                    "message": f'"{business_name}" is available on the Virginia SCC database!',
                    "conflicts": [],
                    "raw": body_text
                }

            else:
                return {
                    "available": None,
                    "status": "UNKNOWN",
                    "message": "Could not determine availability. Please check the Virginia SCC website directly.",
                    "conflicts": [],
                    "raw": body_text
                }

    except Exception as e:
        return {
            "available": None,
            "status": "ERROR",
            "message": f"Could not connect to Virginia SCC: {str(e)}",
            "conflicts": [],
            "raw": ""
        }

def check_llc_exists_on_scc(business_name: str) -> dict:
    """Verifies an LLC a customer claims to already have is actually on the
    Virginia SCC's public Business Entity Search - used when a customer
    checks "I already have a Virginia LLC" on the intake form, so we don't
    skip formation for a business that was never really filed. This is a
    distinct question from check_name_on_scc's "is this name available to
    register" - here we want an exact match to confirm existence, not a
    distinguishability check against similar names."""
    try:
        with sync_playwright() as p:
            try:
                browser = _connect_to_local_chrome(p)
            except Exception:
                return {
                    "exists": None,
                    "status": "UNAVAILABLE",
                    "message": "We can't verify existing LLCs against Virginia SCC right now.",
                    "link": SCC_ENTITY_SEARCH_URL,
                }
            context = browser.contexts[0]
            page = context.new_page()

            page.goto("https://cis.scc.virginia.gov/Account/Login")
            page.wait_for_load_state("networkidle")
            if page.locator('#txtUsername').count() > 0:
                page.fill('#txtUsername', SCC_USERNAME)
                page.fill('#txtPassword', SCC_PASSWORD)
                page.click('#Login')
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(2000)

            page.goto("https://cis.scc.virginia.gov/EntitySearch/Index")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
            page.select_option('#BEFilingSearch_ddlSearchLogic', value='3')  # Exact Match
            page.fill('#BusinessSearch_Index_txtBusinessName', business_name)
            page.wait_for_timeout(300)
            page.click('#btnSearch')
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)

            body = page.inner_text("body")
            page.close()

            if "no records found" in body.lower():
                return {
                    "exists": False,
                    "message": f'We could not find this LLC in the Virginia SCC database. Please double check the name or uncheck this box to form a new LLC.',
                }

            if business_name.lower() in body.lower():
                return {
                    "exists": True,
                    "message": f'{business_name} found in Virginia SCC records',
                }

            return {
                "exists": None,
                "message": "Could not determine whether this LLC exists. Please check the Virginia SCC website directly.",
            }
    except Exception as e:
        return {
            "exists": None,
            "message": f"Could not connect to Virginia SCC: {str(e)}",
        }
