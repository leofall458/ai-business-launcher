import os
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

SCC_USERNAME = os.getenv("SCC_USERNAME")
SCC_PASSWORD = os.getenv("SCC_PASSWORD")

def file_llc(customer_data: dict):
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://172.27.176.1:9222")
        context = browser.contexts[0]
        page = context.new_page()

        print("📌 Navigating to SCC login...")
        page.goto("https://cis.scc.virginia.gov/Account/Login")
        page.wait_for_load_state("networkidle")

        print("📌 Logging in...")
        page.fill('#txtUsername', SCC_USERNAME)
        page.fill('#txtPassword', SCC_PASSWORD)
        page.click('#Login')
        page.wait_for_load_state("networkidle")

        print("📌 Current URL:", page.url)
        print("📌 Page title:", page.title())
        page.screenshot(path="scc_after_login.png")
        print("✅ Screenshot saved as scc_after_login.png")

        print("\nPress ENTER to close...")
        input()
        browser.close()

if __name__ == "__main__":
    customer = {
        "business_name": "Test Business LLC",
        "full_name": "John Smith",
        "principal_address": "123 Main St, Arlington, VA 22201",
    }
    file_llc(customer)
