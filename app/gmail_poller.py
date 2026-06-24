"""Polls leofall458@gmail.com every 5 minutes for SCC approval emails from
CIS@scc.virginia.gov, matches each one to the Firestore order it confirms,
forwards a personalized copy (with the Certificate of Organization PDF
attached) to the customer, uploads that PDF to private GCS storage, and
advances the order past filing_confirmed - the same transition the admin
dashboard's manual fallback button and the hourly SCC name-search poller
(app/check_scc_status.py) also drive. Whichever of the three notices a
given approval first wins; advance_past_filing_confirmed is idempotent
specifically so the other two are safe no-ops after that.

Needs a real, human-authorized Gmail OAuth2 grant - there's no service
account path for reading someone's personal inbox - so like the other
local-only scripts here, this has to run on this machine, not Cloud Run.
See the setup instructions for how to create gmail_credentials.json.

Run with: python3 -m app.gmail_poller
"""

import base64
import os
import re
import time
import traceback

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.cloud import firestore

from app.main import ORDERS, db, advance_past_filing_confirmed, run_asset_generation
from app.notify import notify_windows
from app.email_service import forward_scc_approval_email
from app.document_store import upload_document

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = "gmail_credentials.json"
TOKEN_FILE = "gmail_token.json"
SCC_SENDER = "CIS@scc.virginia.gov"
POLL_INTERVAL_SECONDS = 300

POLLER_STATUS_DOC = db.collection("system").document("gmail_poller")
PROCESSED_EMAILS = db.collection("processed_scc_emails")

def get_gmail_service():
    """First run opens a browser for the human consent screen and caches
    a refresh token in TOKEN_FILE; every run after that is silent unless
    the grant is revoked. See the setup instructions for CREDENTIALS_FILE -
    it has to come from a real OAuth client created in Cloud Console,
    there's no way to provision Gmail-inbox-reading access by API alone."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"{CREDENTIALS_FILE} not found in the project root. Download OAuth client "
                    "credentials from Google Cloud Console first - see the setup instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)

def _normalize(name: str) -> str:
    """Strips everything but letters/digits and uppercases, so "Executive
    Paws, LLC" and "EXECUTIVE PAWS LLC" compare equal regardless of
    punctuation or case differences between how we stored the name and
    how SCC's own system rendered it back in the email."""
    return re.sub(r"[^A-Z0-9]", "", name.upper())

def find_matching_order(subject: str, body: str):
    """Only orders still in filing_submitted are candidates - SCC's email
    can only ever be about a filing we ourselves already submitted, so
    matching against any other state would either be a stale duplicate or
    a false positive. Returns (order_id, order) or (None, None)."""
    haystack = _normalize(f"{subject} {body}")
    for doc in ORDERS.where("state", "==", "filing_submitted").stream():
        order = doc.to_dict()
        business_name = order.get("business_name", "")
        if business_name and _normalize(business_name) in haystack:
            return doc.id, order
    return None, None

def _get_header(headers: list, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""

def _flatten_parts(parts: list) -> list:
    """Gmail nests multipart/alternative inside multipart/mixed (and so
    on) rather than giving a flat list - this walks the whole tree once
    so callers can just scan for the mime types/filenames they want."""
    flat = []
    for p in parts or []:
        if p.get("parts"):
            flat.extend(_flatten_parts(p["parts"]))
        else:
            flat.append(p)
    return flat

def _extract_body_text(payload: dict) -> str:
    text = ""
    for part in _flatten_parts(payload.get("parts") or [payload]):
        data = part.get("body", {}).get("data")
        if part.get("mimeType") in ("text/plain", "text/html") and data:
            text += base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return text

def _extract_pdf_attachments(service, message_id: str, payload: dict) -> list:
    """Returns [(filename, bytes), ...] for every PDF attached to the
    message - SCC's approval email is expected to carry exactly one (the
    Certificate of Organization), but this doesn't assume that."""
    attachments = []
    for part in _flatten_parts(payload.get("parts") or []):
        filename = part.get("filename", "")
        attachment_id = part.get("body", {}).get("attachmentId")
        if not filename.lower().endswith(".pdf") or not attachment_id:
            continue
        att = service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id
        ).execute()
        attachments.append((filename, base64.urlsafe_b64decode(att["data"])))
    return attachments

# SCC confirmation numbers look like a 15-digit string (e.g.
# 202606237499988) - best-effort extraction only. If this doesn't find
# one, the order is still fully processed; an admin can fill the
# confirmation number in later via the manual fallback if it matters.
CONFIRMATION_NUMBER_RE = re.compile(r"\b(\d{15})\b")

def process_message(service, message_id: str):
    if PROCESSED_EMAILS.document(message_id).get().exists:
        return

    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    subject = _get_header(headers, "Subject")
    body_text = _extract_body_text(payload)

    log_entry = {
        "gmail_message_id": message_id,
        "subject": subject,
        "processed_at": firestore.SERVER_TIMESTAMP,
        "matched": False,
        "forwarded": False,
    }

    order_id, order = find_matching_order(subject, body_text)
    if not order_id:
        print(f"⚠️ SCC email '{subject}' didn't match any order in filing_submitted - left for manual review.")
        log_entry["error"] = "No matching order in filing_submitted state."
        PROCESSED_EMAILS.document(message_id).set(log_entry)
        return

    business_name = order.get("business_name", "")
    log_entry["matched"] = True
    log_entry["order_id"] = order_id
    log_entry["business_name"] = business_name
    print(f"✅ Matched SCC approval email to order {order_id} ({business_name})")

    attachments = _extract_pdf_attachments(service, message_id, payload)
    certificate_bytes = attachments[0][1] if attachments else None
    if not certificate_bytes:
        print(f"⚠️ No PDF attachment found on SCC email for {business_name} - forwarding without one.")

    confirmation_match = CONFIRMATION_NUMBER_RE.search(f"{subject} {body_text}")
    confirmation_number = confirmation_match.group(1) if confirmation_match else None

    order_ref = ORDERS.document(order_id)
    firestore_update = {}
    if confirmation_number:
        firestore_update["scc_confirmation_number"] = confirmation_number
    if certificate_bytes:
        try:
            object_name = upload_document(order_id, certificate_bytes, "application/pdf", "pdf")
            firestore_update["documents.certificate"] = {"object_name": object_name, "uploaded_at": firestore.SERVER_TIMESTAMP}
        except Exception as e:
            print(f"⚠️ Could not upload certificate for order {order_id}: {e}")
            log_entry["error"] = f"Certificate upload failed: {e}"
    if firestore_update:
        order_ref.set(firestore_update, merge=True)

    try:
        log_entry["forwarded"] = forward_scc_approval_email(order, certificate_bytes)
    except Exception as e:
        print(f"⚠️ Could not forward SCC approval email for order {order_id}: {e}")
        log_entry["error"] = f"Forward failed: {e}"

    trigger_assets = advance_past_filing_confirmed(order_ref, order)
    if trigger_assets:
        run_asset_generation(order_id)

    PROCESSED_EMAILS.document(message_id).set(log_entry)
    notify_windows("Launch Bridge LLC", f"{business_name}: SCC approval email processed and forwarded.")
    print(f"🎉 Order {order_id} ({business_name}) advanced past filing_confirmed via Gmail poller.")

def poll_once(service):
    try:
        results = service.users().messages().list(
            userId="me", q=f"from:{SCC_SENDER}", maxResults=20
        ).execute()
    except Exception as e:
        POLLER_STATUS_DOC.set({
            "last_checked_at": firestore.SERVER_TIMESTAMP,
            "status": "error", "last_error": str(e),
        }, merge=True)
        raise

    POLLER_STATUS_DOC.set({"last_checked_at": firestore.SERVER_TIMESTAMP, "status": "ok"}, merge=True)

    for m in results.get("messages", []):
        try:
            process_message(service, m["id"])
        except Exception as e:
            print(f"⚠️ Could not process Gmail message {m['id']}: {e}")
            traceback.print_exc()

def main():
    print(f"🔁 Gmail poller started (checking every {POLL_INTERVAL_SECONDS}s) - Ctrl+C to stop")
    service = get_gmail_service()
    while True:
        try:
            poll_once(service)
        except Exception as e:
            print(f"⚠️ Gmail poller loop crashed: {e}")
            traceback.print_exc()
        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Stopped.")
