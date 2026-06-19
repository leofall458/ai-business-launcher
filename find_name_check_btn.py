import os
import json
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=300)
    context = browser.new_context()

    with open("scc_session.json", "r") as f:
        cookies = json.load(f)
    context.add_cookies(cookies)

    page = context.new_page()

    # Step 1
    page.goto("https://cis.scc.virginia.gov/BusinesEntityOnlineShared/CreateBusiness?categoryID=Form%20or%20Register%20a%20Business")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    page.click('#rdDomesticEntity')
    page.wait_for_timeout(500)
    page.click('#btnContinue')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    # Step 2
    page.select_option('#entityType', label='Limited Liability Company')
    page.wait_for_timeout(1000)
    page.select_option('#filingType', value='25')
    page.wait_for_timeout(1000)
    page.click('#btnContinue')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)

    # Select no reservation
    page.click('#nameReservationNo')
    page.wait_for_timeout(500)

    # Print all buttons on the page
    buttons = page.query_selector_all("input[type=button], button, input[type=submit]")
    print("Buttons on page:")
    for btn in buttons:
        print(f"  id={btn.get_attribute('id')} name={btn.get_attribute('name')} value={btn.get_attribute('value')} text={btn.inner_text()}")

    page.screenshot(path="scc_name_step.png")
    print("✅ Screenshot saved")

    input("\nPress ENTER to close...")
    browser.close()
