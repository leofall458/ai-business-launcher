"""Every 30 minutes, logs into the Virginia SCC Clerk's Information
System (CIS) as Leo and reads his own authenticated dashboard instead of
searching the public Entity Search one business name at a time. Confirmed
live against the real CIS site (not guessed) before writing this:

- Business Entity Submissions (SCC's dashboard tile is labelled that, but
  its own "View All" link actually points at /OnlineDashboard/
  RejectedFilings - confirmed by inspecting the tile's real href, not
  assumed from the label) lists every filing Leo has ever submitted with
  a literal Status column ("Approved" once SCC has processed it - a
  hidden column on the same row independently shows the entity's own
  status as "Active", confirming the two terms mean the same thing here).
- Correspondence (/OnlineDashboard/Correspondence) lists the actual
  documents SCC issued per entity - Certificate, Acceptance Letter,
  Commission Order. The Certificate link's onclick reveals a direct,
  session-authenticated PDF URL (/CommonHelper/
  DocumentStorageLocalFileget?DocumentId=...) that downloads the real
  Certificate of Organization via Playwright's own request context - no
  email needed, no window/tab has to actually open.
- Recent Payments (/OnlineDashboard/PaidFilings) is the only place the
  customer-facing confirmation number itself appears (its "Work Order
  Number" column, e.g. 202606237499988) - not on the submissions or
  correspondence pages - so it's scraped separately, keyed by entity name.

Cross-references each "Approved" entity name against Firestore orders
still in filing_submitted. The admin dashboard's manual "Mark Approved
by SCC" fallback watches the same target state; advance_past_filing_confirmed
is idempotent, so whichever notices a real approval first is a safe
no-op for the other rather than a double-send.

Requires the same local Chrome/CDP access as everything else here (see
CDP_URL below), so it has to run on this machine, not on Cloud Run.

Run with: python3 -m app.check_scc_status
"""

import re
import time
import traceback

from playwright.sync_api import sync_playwright
from google.cloud import firestore

from app.main import ORDERS, advance_past_filing_confirmed, run_asset_generation
from app.notify import notify_windows
from app.email_service import send_llc_approved_email
from app.document_store import upload_document
from app.secrets import get_secret

CDP_URL = "http://172.27.176.1:9222"
SCC_USERNAME = get_secret("SCC_USERNAME")
SCC_PASSWORD = get_secret("SCC_PASSWORD")
CHECK_INTERVAL_SECONDS = 1800

APPROVED_STATUSES = {"Approved", "Active"}

def _normalize(name: str) -> str:
    """Strips everything but letters/digits and uppercases, so minor
    punctuation/spacing differences between how we stored a name and how
    SCC's own dashboard renders it back don't break the match."""
    return re.sub(r"[^A-Z0-9]", "", name.upper())

def _login(page):
    page.goto("https://cis.scc.virginia.gov/Account/Login")
    page.wait_for_load_state("networkidle")
    if page.locator("#txtUsername").count() > 0:
        page.fill("#txtUsername", SCC_USERNAME)
        page.fill("#txtPassword", SCC_PASSWORD)
        page.click("#Login")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)
    if "Hi," not in page.inner_text("body"):
        raise RuntimeError("SCC login failed - check SCC_USERNAME/SCC_PASSWORD.")

def _scrape_paginated_table(page, max_pages: int = 20) -> list:
    """Every grid on this dashboard (submissions, payments, correspondence)
    shares the same $xhtmlMain pagination widget - the "Next >" link sits
    inside <li class="next"> with a clickable <a> until the last page,
    where it becomes <li class="next-off"> with plain text and no link.
    Returns a flat list of rows, each row a list of {text, link_onclick}
    cells, across every page."""
    rows = []
    for _ in range(max_pages):
        if page.locator("table tbody tr").count() == 0:
            break
        rows.extend(page.eval_on_selector_all("table tbody tr", """
            trs => trs.map(tr => Array.from(tr.querySelectorAll('td')).map(td => ({
                text: td.innerText.trim(),
                link_onclick: td.querySelector('a') ? td.querySelector('a').getAttribute('onclick') : null,
            })))
        """))
        next_link = page.locator("li.next a")
        if next_link.count() == 0:
            break
        next_link.click()
        page.wait_for_timeout(1500)
    return rows

def _scrape_submissions(page) -> list:
    """[{"entity_name": ..., "status": ...}, ...] for every filing on
    Leo's Business Entity Submissions dashboard."""
    page.goto("https://cis.scc.virginia.gov/OnlineDashboard/RejectedFilings")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    rows = _scrape_paginated_table(page)
    return [{"entity_name": r[0]["text"], "status": r[3]["text"]} for r in rows if len(r) > 3]

def _scrape_confirmation_numbers(page) -> dict:
    """Normalized entity name -> SCC work order/confirmation number, from
    Recent Payments - the only dashboard page that shows it."""
    page.goto("https://cis.scc.virginia.gov/OnlineDashboard/PaidFilings")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    rows = _scrape_paginated_table(page)
    return {_normalize(r[0]["text"]): r[1]["text"] for r in rows if len(r) > 1 and r[0]["text"]}

DOCUMENT_URL_RE = re.compile(r'GetFilingDocument\("([^"]+)"\)')

def _fetch_certificate(context, page, entity_name: str) -> bytes:
    """Finds the Certificate row for entity_name on the Correspondence
    dashboard and downloads it directly. GetFilingDocument() just does
    window.open(url) in the browser - that URL itself returns the raw
    PDF given the session's own cookies (confirmed: status 200,
    content-type application/pdf), so there's no tab or download event
    to deal with, just an authenticated GET via the same browser
    context's request API."""
    page.goto("https://cis.scc.virginia.gov/OnlineDashboard/Index")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    page.click('a[href="/OnlineDashboard/Correspondence"]')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)

    rows = _scrape_paginated_table(page)
    for r in rows:
        if len(r) < 3:
            continue
        row_entity, description = r[0]["text"], r[2]["text"]
        if description != "Certificate" or _normalize(row_entity) != _normalize(entity_name):
            continue
        onclick = r[2]["link_onclick"] or ""
        match = DOCUMENT_URL_RE.search(onclick)
        if not match:
            continue
        url = "https://cis.scc.virginia.gov" + match.group(1)
        resp = context.request.get(url)
        if resp.ok:
            return resp.body()
    return None

def check_once():
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(CDP_URL)
            except Exception:
                print("⚠️ No local Chrome reachable over CDP - skipping this check.")
                return

            context = browser.contexts[0]
            page = context.new_page()
            _login(page)

            submissions = _scrape_submissions(page)
            approved_names = {_normalize(s["entity_name"]) for s in submissions if s["status"] in APPROVED_STATUSES}
            print(f"📋 SCC dashboard shows {len(submissions)} submission(s), {len(approved_names)} approved.")

            confirmation_numbers = None  # scraped lazily - only if something actually matches

            for doc in ORDERS.where("state", "==", "filing_submitted").stream():
                order_id = doc.id
                order = doc.to_dict()
                business_name = order.get("business_name", "")
                if not business_name or _normalize(business_name) not in approved_names:
                    continue

                print(f"✅ {business_name} is now Approved on the Virginia SCC dashboard!")

                if confirmation_numbers is None:
                    confirmation_numbers = _scrape_confirmation_numbers(page)
                confirmation_number = confirmation_numbers.get(_normalize(business_name), "")

                certificate_bytes = None
                try:
                    certificate_bytes = _fetch_certificate(context, page, business_name)
                except Exception as e:
                    print(f"⚠️ Could not fetch certificate for {business_name}: {e}")

                order_ref = doc.reference
                firestore_update = {}
                if confirmation_number:
                    firestore_update["scc_confirmation_number"] = confirmation_number
                if certificate_bytes:
                    try:
                        object_name = upload_document(order_id, certificate_bytes, "application/pdf", "pdf")
                        firestore_update["documents.certificate"] = {"object_name": object_name, "uploaded_at": firestore.SERVER_TIMESTAMP}
                    except Exception as e:
                        print(f"⚠️ Could not upload certificate for order {order_id}: {e}")
                if firestore_update:
                    # .update(), not .set(merge=True) - only .update() treats
                    # the dotted "documents.certificate" key above as a
                    # nested field path rather than a literal dotted name.
                    order_ref.update(firestore_update)

                trigger_assets = advance_past_filing_confirmed(order_ref, order)
                send_llc_approved_email(order, order_id, confirmation_number)
                if trigger_assets:
                    run_asset_generation(order_id)
                notify_windows("Launch Bridge LLC", f"{business_name} approved by Virginia SCC!")

            page.close()
    except Exception as e:
        print(f"⚠️ SCC dashboard check crashed: {e}")
        traceback.print_exc()

def main():
    print(f"🔁 SCC status checker started (checking every {CHECK_INTERVAL_SECONDS}s) - Ctrl+C to stop")
    while True:
        check_once()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Stopped.")
