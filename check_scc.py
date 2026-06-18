import os
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://172.27.176.1:9222")
    context = browser.contexts[0]
    page = context.new_page()

    page.goto("https://cis.scc.virginia.gov/Account/Login")
    page.wait_for_load_state("networkidle")

    # Print all input fields on the page
    inputs = page.query_selector_all("input")
    print("Input fields found:")
    for inp in inputs:
        print(f"  name={inp.get_attribute('name')} id={inp.get_attribute('id')} type={inp.get_attribute('type')}")

    page.screenshot(path="scc_login.png")
    print("\n✅ Screenshot saved as scc_login.png")
    input("Press ENTER to close...")
    browser.close()
