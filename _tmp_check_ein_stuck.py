from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://172.27.176.1:9222")
    context = browser.contexts[0]
    page = None
    for pg in context.pages:
        if "irs.gov" in pg.url:
            page = pg
    if page:
        page.bring_to_front()
        print("URL:", page.url)
        page.screenshot(path="/tmp/ein_stuck.png", full_page=True)
    else:
        print("no IRS tab found")
    browser.close()
