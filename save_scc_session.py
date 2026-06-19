import json
from playwright.sync_api import sync_playwright

print("⚠️  Make sure you are already logged into SCC in Chrome")
print("Then press ENTER...")
input()

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://172.27.176.1:9222")
    context = browser.contexts[0]
    
    cookies = context.cookies()
    with open("scc_session.json", "w") as f:
        json.dump(cookies, f)
    
    print(f"✅ Saved {len(cookies)} cookies to scc_session.json")
    browser.close()
