import os
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

SCC_USERNAME = os.getenv("SCC_USERNAME")
SCC_PASSWORD = os.getenv("SCC_PASSWORD")

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

    # Search for a name
    page.fill('#txtEntityName', "Paws LLC")
    page.click('#Check_Name')
    
    # Wait for "Please wait..." to disappear
    page.wait_for_function("!document.body.innerText.includes('Please wait')", timeout=15000)
    page.wait_for_timeout(2000)

    print("=== PAGE TEXT AFTER RESULT LOADED ===")
    print(page.inner_text("body"))
    print("=== END ===")

    page.screenshot(path="scc_result.png")
    print("\n✅ Screenshot saved")
    page.close()
    browser.close()
