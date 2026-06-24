"""Encrypted-at-rest SSN storage in Firestore, encrypted/decrypted through
Cloud KMS - replaces the previous in-memory-only design (a plain process
dict, lost on every restart) so an SSN survives a server restart between
collection and EIN filing, while still never touching disk or a log line
in plaintext.

Only the base64-encoded ciphertext (ssn_encrypted) is ever written to
Firestore. KMS's own encrypt/decrypt calls are the only place plaintext SSN
bytes exist outside of the original HTTP request body and the IRS filer's
browser form fields.

Uses its own Firestore client/collection (same pattern as
app/document_store.py) rather than importing app.main, so app.main can
import this module without a circular import.
"""

import base64
import datetime

from google.cloud import firestore
from google.cloud import kms

from app.config import FIREBASE_PROJECT_ID, ORDERS_COLLECTION
from app.log_scrub import scrub_ssn

KMS_PROJECT = "ai-biz-launcher"
KMS_LOCATION = "us-east4"
KMS_KEYRING = "launchbridge-vault"
KMS_KEY = "ssn-key"

_kms_client = kms.KeyManagementServiceClient()
_key_name = _kms_client.crypto_key_path(KMS_PROJECT, KMS_LOCATION, KMS_KEYRING, KMS_KEY)

_db = firestore.Client(project=FIREBASE_PROJECT_ID)
_ORDERS = _db.collection(ORDERS_COLLECTION)

def encrypt_ssn(ssn: str, order_id: str) -> bool:
    """Encrypts ssn with Cloud KMS and stores only the resulting base64
    ciphertext on the order, plus when it was collected (used by
    ssn_age_hours for the 72-hour expiry sweep). Clears any stale
    ssn_expired flag from a previous re-entry cycle."""
    try:
        response = _kms_client.encrypt(request={"name": _key_name, "plaintext": ssn.encode("utf-8")})
        ciphertext_b64 = base64.b64encode(response.ciphertext).decode("ascii")
        _ORDERS.document(order_id).set({
            "ssn_encrypted": ciphertext_b64,
            "ssn_collected_at": firestore.SERVER_TIMESTAMP,
            "ssn_expired": False,
        }, merge=True)
        return True
    except Exception as e:
        print(scrub_ssn(f"⚠️ Could not encrypt/store SSN for order {order_id}: {e}"))
        return False

def decrypt_ssn(order_id: str) -> str:
    """Returns the plaintext SSN for one-time use by the EIN filer, or ""
    if nothing is stored or decryption fails. The returned value must
    never be logged by any caller."""
    order = _ORDERS.document(order_id).get().to_dict() or {}
    ciphertext_b64 = order.get("ssn_encrypted")
    if not ciphertext_b64:
        return ""
    try:
        ciphertext = base64.b64decode(ciphertext_b64)
        response = _kms_client.decrypt(request={"name": _key_name, "ciphertext": ciphertext})
        return response.plaintext.decode("utf-8")
    except Exception as e:
        print(scrub_ssn(f"⚠️ Could not decrypt SSN for order {order_id}: {e}"))
        return ""

def delete_ssn(order_id: str) -> bool:
    """Removes the encrypted SSN for good - called once EIN filing
    actually succeeds, or by the 72-hour expiry sweep. Logs that a
    deletion happened (never the value) to the order's events
    subcollection, the same audit trail app.main.record_state writes to."""
    try:
        order_ref = _ORDERS.document(order_id)
        order_ref.set({
            "ssn_encrypted": firestore.DELETE_FIELD,
            "ssn_collected_at": firestore.DELETE_FIELD,
        }, merge=True)
        order_ref.collection("events").add({"event": "ssn_deleted", "at": firestore.SERVER_TIMESTAMP})
        return True
    except Exception as e:
        print(f"⚠️ Could not delete SSN for order {order_id}: {e}")
        return False

def is_ssn_stored(order_id: str) -> bool:
    order = _ORDERS.document(order_id).get().to_dict() or {}
    return bool(order.get("ssn_encrypted"))

def ssn_age_hours(order_id: str) -> float:
    """Hours since ssn_collected_at. Returns float("inf") if it was never
    set, so a caller comparing against an expiry threshold treats a
    missing timestamp the same as "definitely expired" instead of
    crashing on it."""
    order = _ORDERS.document(order_id).get().to_dict() or {}
    collected_at = order.get("ssn_collected_at")
    if not collected_at:
        return float("inf")
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - collected_at).total_seconds() / 3600
