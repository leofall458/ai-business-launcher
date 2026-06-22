"""Replaces clicking 'Approve' and the EIN auto-retry with a loop you run
on your own machine. Both of those steps drive Playwright against a real,
already-logged-in Chrome browser over CDP (see CDP_URL in scc_llc_filer.py)
- a private LAN address only reachable from this machine, never from the
deployed Cloud Run service. So the customer-facing site can live on Cloud
Run, but this loop has to run here, with Chrome open and logged into SCC
and the IRS site.

Calls the exact same run_scc_filing/run_ein_filing functions the admin
dashboard's buttons call - this is just a different trigger for them, not
a reimplementation. Importing app.main does not start its web server (no
uvicorn.run in that module) and does not fire its @app.on_event("startup")
hook, so this is safe to run standalone alongside (or instead of) the
deployed app.

Run with: python3 -m app.local_filing_poller
"""

import time
import traceback

from google.cloud import firestore

from app.main import db, run_scc_filing, run_ein_filing, EIN_ELIGIBLE_STATES

POLL_INTERVAL_SECONDS = 60

def process_once():
    for doc in db.collection("orders").where("state", "==", "name_cleared").stream():
        order_id = doc.id
        print(f"📋 Auto-approving order {order_id} for SCC filing...")
        doc.reference.set({
            "state": "review_approved", "review_approved_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        try:
            run_scc_filing(order_id)
        except Exception as e:
            print(f"⚠️ run_scc_filing crashed for {order_id}: {e}")
            traceback.print_exc()

    for doc in db.collection("orders").where("state", "in", list(EIN_ELIGIBLE_STATES)).stream():
        order_id = doc.id
        print(f"📋 Checking EIN filing for order {order_id}...")
        try:
            run_ein_filing(order_id)
        except Exception as e:
            print(f"⚠️ run_ein_filing crashed for {order_id}: {e}")
            traceback.print_exc()

def main():
    print(f"🔁 Local filing poller started (checking every {POLL_INTERVAL_SECONDS}s) - Ctrl+C to stop")
    while True:
        try:
            process_once()
        except Exception as e:
            print(f"⚠️ Poller loop crashed: {e}")
            traceback.print_exc()
        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Stopped.")
