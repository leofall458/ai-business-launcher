import re
import time
import base64
import secrets
import requests
from google.cloud import firestore
from app.config import GITHUB_TOKEN, GITHUB_USERNAME, FIREBASE_PROJECT_ID, ORDERS_COLLECTION

GITHUB_API = "https://api.github.com"

def _headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def slugify_repo_name(business_name: str) -> str:
    """GitHub repo names: letters/numbers/hyphens/underscores/periods only.
    Append a random suffix to avoid collisions with other customers' sites."""
    slug = re.sub(r"[^a-z0-9]+", "-", business_name.lower()).strip("-")
    slug = slug[:80].strip("-") or "business"
    suffix = secrets.token_hex(3)
    return f"{slug}-{suffix}"

def create_repo(repo_name: str) -> bool:
    resp = requests.post(
        f"{GITHUB_API}/user/repos",
        headers=_headers(),
        json={"name": repo_name, "private": False, "auto_init": True},
        timeout=30,
    )
    if resp.status_code != 201:
        print(f"⚠️ Could not create GitHub repo {repo_name}: {resp.status_code} {resp.text}")
        return False
    return True

def push_index_html(repo_name: str, html: str) -> bool:
    content_b64 = base64.b64encode(html.encode()).decode()
    url = f"{GITHUB_API}/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html"

    for attempt in range(3):
        resp = requests.put(
            url,
            headers=_headers(),
            json={"message": "Deploy website", "content": content_b64, "branch": "main"},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            return True
        if resp.status_code == 404 and attempt < 2:
            time.sleep(1.5)
            continue
        print(f"⚠️ Could not push index.html to {repo_name}: {resp.status_code} {resp.text}")
        return False
    return False

def enable_pages(repo_name: str) -> bool:
    resp = requests.post(
        f"{GITHUB_API}/repos/{GITHUB_USERNAME}/{repo_name}/pages",
        headers=_headers(),
        json={"source": {"branch": "main", "path": "/"}},
        timeout=30,
    )
    if resp.status_code not in (201, 204):
        print(f"⚠️ Could not enable GitHub Pages on {repo_name}: {resp.status_code} {resp.text}")
        return False
    return True

def wait_for_pages_build(repo_name: str, timeout_seconds: int = 90, poll_interval: int = 5) -> bool:
    """A successful enable_pages() call only means GitHub accepted the
    request - the actual build is asynchronous and takes 30-90s the first
    time a repo's Pages site goes live (confirmed by polling a real test
    deploy: 404 at 10s, 200 only after ~40s), so handing back the URL
    right after that call means handing out a link that still 404s. Polls
    GitHub's own Pages-status endpoint until it reports "built" instead.

    GitHub's legacy Pages pipeline can also flake on a repo's very first
    build - confirmed on a real deploy where the first build errored with
    a generic "Page build failed." but a second, separate build GitHub
    queued on its own then succeeded seconds later, no code-side retry
    involved. Request one explicit rebuild via the dedicated builds
    endpoint before giving up, rather than hoping that happens again."""
    status_url = f"{GITHUB_API}/repos/{GITHUB_USERNAME}/{repo_name}/pages"
    builds_url = f"{GITHUB_API}/repos/{GITHUB_USERNAME}/{repo_name}/pages/builds"
    deadline = time.time() + timeout_seconds
    retried = False
    while time.time() < deadline:
        resp = requests.get(status_url, headers=_headers(), timeout=30)
        if resp.status_code == 200:
            status = resp.json().get("status")
            if status == "built":
                return True
            if status in ("errored", "deployment_failed"):
                if not retried:
                    print(f"⚠️ GitHub Pages build errored for {repo_name} - requesting a rebuild...")
                    requests.post(builds_url, headers=_headers(), timeout=30)
                    retried = True
                else:
                    print(f"⚠️ GitHub Pages build failed again for {repo_name} after retry: {status}")
                    return False
        time.sleep(poll_interval)
    print(f"⚠️ GitHub Pages build for {repo_name} did not finish within {timeout_seconds}s")
    return False

def get_index_html_sha(repo_name: str) -> str:
    resp = requests.get(
        f"{GITHUB_API}/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html",
        headers=_headers(),
        params={"ref": "main"},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"⚠️ Could not read index.html sha for {repo_name}: {resp.status_code} {resp.text}")
        return None
    return resp.json()["sha"]

def update_index_html(repo_name: str, html: str) -> bool:
    """Overwrites an already-deployed site's index.html (e.g. once the real
    Stripe Payment Link is ready and needs to replace the mailto fallback).
    The Contents API requires the current file's sha to update it."""
    sha = get_index_html_sha(repo_name)
    if not sha:
        return False

    content_b64 = base64.b64encode(html.encode()).decode()
    resp = requests.put(
        f"{GITHUB_API}/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html",
        headers=_headers(),
        json={"message": "Update website", "content": content_b64, "branch": "main", "sha": sha},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"⚠️ Could not update index.html for {repo_name}: {resp.status_code} {resp.text}")
        return False
    return True

def save_website_to_order(order_id: str, url: str, repo_name: str):
    try:
        db = firestore.Client(project=FIREBASE_PROJECT_ID)
        db.collection(ORDERS_COLLECTION).document(order_id).set(
            {"website_url": url, "website_repo": repo_name}, merge=True
        )
    except Exception as e:
        print(f"⚠️ Could not save website URL to Firestore order {order_id}: {e}")

def deploy_website(business_name: str, html_content: str, order_id: str = None) -> dict:
    """Deploys the given HTML as a new GitHub Pages site under the
    launchbridge-sites GitHub account. Returns {"url": ..., "repo": ...},
    or None if deployment failed. If order_id is given, saves the URL and
    repo name onto that order's Firestore document."""
    repo_name = slugify_repo_name(business_name)

    if not create_repo(repo_name):
        return None
    if not push_index_html(repo_name, html_content):
        return None
    if not enable_pages(repo_name):
        return None
    if not wait_for_pages_build(repo_name):
        return None

    url = f"https://{GITHUB_USERNAME}.github.io/{repo_name}"

    if order_id:
        save_website_to_order(order_id, url, repo_name)

    return {"url": url, "repo": repo_name}
