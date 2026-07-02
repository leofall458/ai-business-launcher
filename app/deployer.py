import re
import gzip
import hashlib
import io
import requests
import google.auth
import google.auth.transport.requests
from google.cloud import firestore, storage
from app.config import FIREBASE_PROJECT_ID, ORDERS_COLLECTION

FIREBASE_HOSTING_BASE = "https://firebasehosting.googleapis.com/v1beta1"
GCS_BUCKET = "ai-biz-launcher-site-bundles"

_VERSION_CONFIG = {
    "config": {
        "headers": [{
            "glob": "**",
            "headers": {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "X-XSS-Protection": "1; mode=block",
            },
        }],
        "rewrites": [{"glob": "**", "path": "/index.html"}],
    }
}

def make_site_id(business_name: str, order_id: str) -> str:
    slug = business_name.lower()
    slug = re.sub(r"\bl\.?l\.?c\.?\b", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    slug = slug[:30].rstrip("-") or "business"
    if not slug[0].isalpha():
        slug = "lb-" + slug[:27]
    suffix = re.sub(r"[^a-z0-9]", "", order_id[:8].lower()) or "00000000"
    return f"{slug}-{suffix}"[:39]

def _auth_headers() -> dict:
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/firebase.hosting",
                "https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    return {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }

def _gzip_file(content: bytes) -> tuple[bytes, str]:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(content)
    compressed = buf.getvalue()
    sha = hashlib.sha256(compressed).hexdigest()
    return compressed, sha

def _save_to_gcs(order_id: str, html_content: str) -> None:
    try:
        client = storage.Client(project=FIREBASE_PROJECT_ID)
        blob = client.bucket(GCS_BUCKET).blob(f"sites/{order_id}/index.html")
        blob.upload_from_string(html_content.encode(), content_type="text/html")
        print(f"[deployer] Saved HTML backup to GCS for order {order_id}")
    except Exception as e:
        print(f"[deployer] ⚠️ GCS backup failed for order {order_id}: {e}")

def _get_gcs_html(order_id: str) -> str | None:
    try:
        client = storage.Client(project=FIREBASE_PROJECT_ID)
        blob = client.bucket(GCS_BUCKET).blob(f"sites/{order_id}/index.html")
        return blob.download_as_text()
    except Exception as e:
        print(f"[deployer] ⚠️ GCS read failed for order {order_id}: {e}")
        return None

def _firebase_create_version_and_release(site_id: str, files: dict[str, bytes]) -> None:
    """Steps 2–6 of Firebase Hosting deployment (create version, populate, upload, finalize, release)."""
    headers = _auth_headers()
    base = FIREBASE_HOSTING_BASE

    # Create version
    resp = requests.post(
        f"{base}/sites/{site_id}/versions",
        headers=headers,
        json=_VERSION_CONFIG,
        timeout=30,
    )
    resp.raise_for_status()
    version_name = resp.json()["name"]
    version_id = version_name.split("/")[-1]

    # Compute gzipped content + SHA256 for each file
    file_hashes: dict[str, str] = {}
    file_data: dict[str, bytes] = {}
    for path, content_bytes in files.items():
        compressed, sha = _gzip_file(content_bytes)
        file_hashes[path] = sha
        file_data[sha] = compressed

    # Populate files
    resp = requests.post(
        f"{base}/sites/{site_id}/versions/{version_id}:populateFiles",
        headers=headers,
        json={"files": file_hashes},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    upload_url = data["uploadUrl"]
    required_hashes = set(data.get("uploadRequiredHashes", []))

    # Upload gzipped content for each required hash
    bearer = headers["Authorization"]
    for sha in required_hashes:
        requests.post(
            f"{upload_url}/{sha}",
            headers={"Authorization": bearer, "Content-Type": "application/octet-stream"},
            data=file_data[sha],
            timeout=60,
        ).raise_for_status()

    # Finalize version
    requests.patch(
        f"{base}/sites/{site_id}/versions/{version_id}",
        headers=headers,
        params={"updateMask": "status"},
        json={"status": "FINALIZED"},
        timeout=30,
    ).raise_for_status()

    # Create release
    requests.post(
        f"{base}/sites/{site_id}/releases",
        headers=headers,
        params={"versionName": version_name},
        json={},
        timeout=30,
    ).raise_for_status()

def _build_site_files(site_id: str, html_content: str) -> dict[str, bytes]:
    url = f"https://{site_id}.web.app"
    robots = f"User-agent: *\nAllow: /\nSitemap: {url}/sitemap.xml\n"
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f'<url><loc>{url}/</loc><changefreq>monthly</changefreq><priority>1.0</priority></url>'
        "</urlset>"
    )
    return {
        "/index.html": html_content.encode(),
        "/robots.txt": robots.encode(),
        "/sitemap.xml": sitemap.encode(),
    }

def deploy_to_firebase(site_id: str, html_content: str) -> str:
    """Create (or update) a Firebase Hosting site and deploy HTML. Returns the live URL."""
    headers = _auth_headers()
    base = FIREBASE_HOSTING_BASE

    # Create site — 409 means it already exists, which is fine for re-deploys
    resp = requests.post(
        f"{base}/projects/{FIREBASE_PROJECT_ID}/sites",
        headers=headers,
        params={"siteId": site_id},
        json={},
        timeout=30,
    )
    if resp.status_code not in (200, 409):
        resp.raise_for_status()

    files = _build_site_files(site_id, html_content)
    _firebase_create_version_and_release(site_id, files)
    return f"https://{site_id}.web.app"

def update_firebase_site(site_id: str, html_content: str) -> bool:
    """Push a new version to an existing Firebase Hosting site."""
    try:
        files = _build_site_files(site_id, html_content)
        _firebase_create_version_and_release(site_id, files)
        return True
    except Exception as e:
        print(f"[deployer] ⚠️ Firebase update failed for {site_id}: {e}")
        return False

def save_website_to_order(order_id: str, url: str, site_id: str) -> None:
    try:
        db = firestore.Client(project=FIREBASE_PROJECT_ID)
        db.collection(ORDERS_COLLECTION).document(order_id).set(
            {
                "website_url": url,
                "website_site_id": site_id,
                "website_hosting": "firebase",
                "website_deployed_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
    except Exception as e:
        print(f"[deployer] ⚠️ Could not save website to Firestore order {order_id}: {e}")

def deploy_website(business_name: str, html_content: str, order_id: str = None) -> dict | None:
    """Deploy HTML to Firebase Hosting. Returns {"url": ..., "site_id": ...} or None on failure.
    If order_id is given, saves URL + site_id to Firestore and backs up HTML to GCS."""
    site_id = make_site_id(business_name, order_id or "00000000")
    try:
        url = deploy_to_firebase(site_id, html_content)
        print(f"[deployer] ✅ Deployed {site_id} → {url}")
    except Exception as e:
        print(f"[deployer] ⚠️ Firebase deploy failed for {site_id}: {e}")
        return None

    if order_id:
        _save_to_gcs(order_id, html_content)
        save_website_to_order(order_id, url, site_id)

    return {"url": url, "site_id": site_id}

def get_website_html(order_id: str) -> str | None:
    """Retrieve stored HTML from GCS for download."""
    return _get_gcs_html(order_id)
