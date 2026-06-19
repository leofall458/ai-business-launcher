import os
import json
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=500)
    context = browser.new_context()

    # Load saved cookies
    with open("scc_session.json", "r") as f:
        cookies = json.load(f)
    context.add_cookies(cookies)
    print("✅ Loaded saved session")

    page = context.new_page()

    # Step 1
    print("📌 Step 1: Virginia Entity...")
    page.goto("https://cis.scc.virginia.gov/BusinesEntityOnlineShared/CreateBusiness?categoryID=Form%20or%20Register%20a%20Business")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    page.click('#rdDomesticEntity')
    page.wait_for_timeout(1000)
    page.click('#btnContinue')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    # Step 2 - LLC
    print("📌 Step 2: LLC + Articles of Organization...")
    page.select_option('#entityType', label='Limited Liability Company')
    page.wait_for_timeout(1000)
    page.select_option('#filingType', value='25')
    page.wait_for_timeout(1000)
    page.click('#btnContinue')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    # Step 3 - Print what we see
    print("URL:", page.url)
    print("\nPage text:")
    print(page.inner_text("body")[:2000])

    inputs = page.query_selector_all("input, select, textarea")
    print("\nForm fields:")
    for el in inputs:
        tag = el.evaluate("el => el.tagName")
        print(f"  {tag} name={el.get_attribute('name')} id={el.get_attribute('id')} type={el.get_attribute('type')}")

    page.screenshot(path="scc_step3.png")
    print("\n✅ Screenshot saved")

    input("\nPress ENTER to close...")
    browser.close()
