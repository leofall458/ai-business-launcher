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

from google.cloud import storage

from app.config import FIREBASE_PROJECT_ID, STORAGE_BUCKET

_client = storage.Client(project=FIREBASE_PROJECT_ID)

def upload_document(order_id: str, content_bytes: bytes, content_type: str, extension: str) -> str:
    """Uploads content under a random object name and returns it for
    storage on the order's documents map."""
    object_name = f"documents/{order_id}/{uuid.uuid4().hex}.{extension}"
    blob = _client.bucket(STORAGE_BUCKET).blob(object_name)
    blob.upload_from_string(content_bytes, content_type=content_type)
    return object_name

def generate_signed_url(object_name: str) -> str:
    """V4 signed URL, 5-minute expiry. Relies on the runtime service
    account holding roles/iam.serviceAccountTokenCreator on itself, since
    there's no private key file present for the default signing path."""
    blob = _client.bucket(STORAGE_BUCKET).blob(object_name)
    return blob.generate_signed_url(version="v4", expiration=datetime.timedelta(minutes=5), method="GET")
