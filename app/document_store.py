"""Generic, randomly-named document storage in the private GCS bucket -
CMEK-encrypted via the bucket's default KMS key (see deploy notes in the
security-rework plan). Object names are never derived from doc_id, order
id structure beyond the order_id prefix, or business name, and the only
caller that ever turns an object_name into a URL is the signed-URL
document route in app/main.py - the URL itself is never logged.

Pre-existing orders' certificate/EIN-letter documents stay on the legacy
deterministic paths in app/storage_service.py; this module is only used
for documents uploaded from this point forward.
"""

import datetime
import uuid

import google.auth
import google.auth.transport.requests
from google.auth import impersonated_credentials
from google.cloud import storage

from app.config import FIREBASE_PROJECT_ID, STORAGE_BUCKET

_client = storage.Client(project=FIREBASE_PROJECT_ID)
_signing_credentials = None

def upload_document(order_id: str, content_bytes: bytes, content_type: str, extension: str) -> str:
    """Uploads content under a random object name and returns it for
    storage on the order's documents map."""
    object_name = f"documents/{order_id}/{uuid.uuid4().hex}.{extension}"
    blob = _client.bucket(STORAGE_BUCKET).blob(object_name)
    blob.upload_from_string(content_bytes, content_type=content_type)
    return object_name

def _get_signing_credentials():
    """Cloud Run's ambient compute-engine credentials carry no private
    key, so the storage client can't sign a URL with them directly - it
    needs to self-impersonate via the IAM Credentials signBlob API
    instead, which is exactly what roles/iam.serviceAccountTokenCreator
    granted to the runtime SA on itself (see the security-rework infra
    checklist) is for. Cached after first use since each one is valid
    for several minutes and building it requires a credential refresh."""
    global _signing_credentials
    if _signing_credentials is not None:
        return _signing_credentials
    credentials, _ = google.auth.default()
    if not credentials.valid:
        credentials.refresh(google.auth.transport.requests.Request())
    sa_email = getattr(credentials, "service_account_email", None)
    if not sa_email or sa_email == "default":
        raise RuntimeError("Could not resolve a service account email to sign with")
    _signing_credentials = impersonated_credentials.Credentials(
        source_credentials=credentials,
        target_principal=sa_email,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        lifetime=300,
    )
    return _signing_credentials

def generate_signed_url(object_name: str) -> str:
    """V4 signed URL, 5-minute expiry."""
    blob = _client.bucket(STORAGE_BUCKET).blob(object_name)
    return blob.generate_signed_url(
        version="v4", expiration=datetime.timedelta(minutes=5), method="GET",
        credentials=_get_signing_credentials(),
    )
