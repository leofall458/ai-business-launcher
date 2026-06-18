import os
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

SCC_USERNAME = os.getenv("SCC_USERNAME")
SCC_PASSWORD = os.getenv("SCC_PASSWORD")

def check_name_on_scc(business_name: str) -> dict:
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://172.27.176.1:9222")
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
