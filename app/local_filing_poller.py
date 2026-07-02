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

Deliberately does NOT also poll "paid" orders to run the SCC name check
itself - the customer clears their own name synchronously in the Step 3
dashboard page (POST /dashboard/orders/{id}/name, using check_name_on_scc
directly) before the order ever reaches "paid" with no name. The real,
authoritative name check still happens for real inside file_llc_on_scc's
own Step 3, right before filing, which this loop does drive with real CDP
access.

Run with: python3 -m app.local_filing_poller
"""

import time
import traceback

from google.cloud import firestore

from app.main import db, ORDERS, run_scc_filing, run_ein_filing, EIN_ELIGIBLE_STATES
from app.notify import notify_windows

POLL_INTERVAL_SECONDS = 60

def _state_now(order_id: str) -> str:
    """run_scc_filing/run_ein_filing don't return a success flag - they
    just update Firestore (see record_state) - so the only reliable way
    to tell whether a call actually advanced an order is to re-read its
    state afterward, not to check a return value."""
    snap = ORDERS.document(order_id).get()
    return snap.to_dict().get("state") if snap.exists else None

def process_once():
    # Retry any order that's already past intake (review_approved) but never
    # actually got filed - most commonly because the admin clicked "Approve"
    # on the live (Cloud Run) site instead of this running, so run_scc_filing
    # crashed trying to reach a CDP address that only exists on this machine.
    # Queried before the intake_complete loop below so an order this same
    # tick auto-approves doesn't also get retried a second time in the same
    # pass.
    for doc in ORDERS.where("state", "==", "review_approved").stream():
        order_id = doc.id
        business_name = doc.to_dict().get("business_name", order_id)
        print(f"📋 Retrying SCC filing for already-approved order {order_id}...")
        try:
            run_scc_filing(order_id)
            if _state_now(order_id) == "filing_submitted":
                notify_windows("Launch Bridge LLC", f"{business_name}: SCC filing submitted and paid.")
        except Exception as e:
            print(f"⚠️ run_scc_filing crashed for {order_id}: {e}")
            traceback.print_exc()

    # Auto-approve orders that have finished intake (Step 5) and are ready to
    # file - name is already cleared back in Step 3. Queried as all three
    # "reached intake_complete but not yet review_approved" states, not just
    # "intake_complete" exactly, because run_early_assets (fired the same
    # moment Step 5 submits) races this poller and can advance the order to
    # assets_generating/assets_complete before the next tick - an exact-match
    # query on "intake_complete" alone would then miss it entirely.
    for doc in ORDERS.where("state", "in", ["intake_complete", "assets_generating", "assets_complete"]).stream():
        order_id = doc.id
        business_name = doc.to_dict().get("business_name", order_id)
        print(f"📋 Auto-approving order {order_id} for SCC filing...")
        doc.reference.set({
            "state": "review_approved", "review_approved_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        try:
            run_scc_filing(order_id)
            if _state_now(order_id) == "filing_submitted":
                notify_windows("Launch Bridge LLC", f"{business_name}: SCC filing submitted and paid.")
        except Exception as e:
            print(f"⚠️ run_scc_filing crashed for {order_id}: {e}")
            traceback.print_exc()

    for doc in ORDERS.where("state", "in", list(EIN_ELIGIBLE_STATES)).stream():
        order_id = doc.id
        business_name = doc.to_dict().get("business_name", order_id)
        print(f"📋 Checking EIN filing for order {order_id}...")
        try:
            run_ein_filing(order_id)
            if _state_now(order_id) == "ein_requested":
                notify_windows("Launch Bridge LLC", f"{business_name}: EIN application submitted to the IRS.")
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
