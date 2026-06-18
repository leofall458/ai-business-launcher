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
    print("📌 Logging in...")
    page.goto("https://cis.scc.virginia.gov/Account/Login")
    page.wait_for_load_state("networkidle")
    page.fill('#txtUsername', SCC_USERNAME)
    page.fill('#txtPassword', SCC_PASSWORD)
    page.click('#Login')
    page.wait_for_load_state("networkidle")
    print("✅ Logged in")

    # Go to name availability check
    print("📌 Navigating to name check...")
    page.goto("https://cis.scc.virginia.gov/Account/NameCheckAvailability")
    page.wait_for_load_state("networkidle")

    print("📌 Current URL:", page.url)
    print("📌 Page title:", page.title())

    # Print all form fields
    inputs = page.query_selector_all("input, select, textarea, button")
    print("\nForm fields found:")
    for el in inputs:
        tag = el.evaluate("el => el.tagName")
        print(f"  {tag} name={el.get_attribute('name')} id={el.get_attribute('id')} type={el.get_attribute('type')} value={el.get_attribute('value')}")

    page.screenshot(path="scc_namesearch.png")
    print("\n✅ Screenshot saved as scc_namesearch.png")
    input("Press ENTER to close...")
    browser.close()
