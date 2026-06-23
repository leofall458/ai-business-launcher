"""Hourly check for whether any submitted LLC filing has actually been
approved by the real Virginia SCC yet - SCC's own processing takes 1-3
business days after filing_submitted and gives no webhook/notification,
so this is the only way to find out short of a human checking manually.
Requires the same local Chrome/CDP access as everything else here (see
CDP_URL in scc_llc_filer.py), so it has to run on this machine, not on
Cloud Run.

Reuses check_llc_exists_on_scc - once SCC actually creates the entity,
it shows up under an exact-name match in their own public Business
Entity Search, which is treated as proof of approval. Reuses
advance_past_filing_confirmed - the exact same function the admin
dashboard's "Mark as Filed" button calls - so this is just a different,
automatic trigger for that already-tested transition, not a
reimplementation. That transition triggers EIN filing eligibility and
(for skip_ein orders) the EIN/website/Stripe pipeline directly, so this
script is what makes the whole order pipeline fully unattended end to
end, with no human checkpoint confirming the real government approval.

Run with: python3 -m app.check_scc_status
"""

import time
import traceback

from app.main import db, ORDERS, advance_past_filing_confirmed, run_asset_generation
from app.agents.scc_name_check import check_llc_exists_on_scc
from app.notify import notify_windows
from app.email_service import send_llc_approved_email

CHECK_INTERVAL_SECONDS = 3600

def check_once():
    for doc in ORDERS.where("state", "==", "filing_submitted").stream():
        order_id = doc.id
        order = doc.to_dict()
        business_name = order.get("business_name", "")
        if not business_name:
            continue

        print(f"🔎 Checking SCC approval status for {business_name} ({order_id})...")
        try:
            result = check_llc_exists_on_scc(business_name)
        except Exception as e:
            print(f"⚠️ Could not check SCC status for {order_id}: {e}")
            traceback.print_exc()
            continue

        if result.get("exists") is True:
            print(f"✅ {business_name} is now approved on Virginia SCC!")
            trigger_assets = advance_past_filing_confirmed(doc.reference, order)
            send_llc_approved_email(order, order_id)
            if trigger_assets:
                run_asset_generation(order_id)
            notify_windows("Launch Bridge LLC", f"{business_name} approved by Virginia SCC!")
        else:
            print(f"⏳ {business_name} not yet showing as approved.")

def main():
    print(f"🔁 SCC status checker started (checking every {CHECK_INTERVAL_SECONDS}s) - Ctrl+C to stop")
    while True:
        try:
            check_once()
        except Exception as e:
            print(f"⚠️ Status checker loop crashed: {e}")
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Stopped.")
