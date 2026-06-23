"""Centralized secret access. Production reads from Google Cloud Secret
Manager; local development (or any environment where Secret Manager isn't
reachable - no ADC, no network, secret not created yet) falls back to the
same-named variable in .env. Every secret is fetched at most once per
process and cached in memory, so normal request traffic never calls out to
Secret Manager - only the first access (or the explicit preload() at
startup) does.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Secrets actually migrated to Secret Manager. Anything not in this list
# (e.g. GEMINI_API_KEY) still just reads from .env - not in scope for this
# migration.
MANAGED_SECRETS = [
    "SCC_USERNAME",
    "SCC_PASSWORD",
    "STRIPE_SECRET_KEY",
    "STRIPE_PUBLISHABLE_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_SECRET_KEY_STAGING",
    "STRIPE_PUBLISHABLE_KEY_STAGING",
    "STRIPE_WEBHOOK_SECRET_STAGING",
    "GITHUB_TOKEN",
    "SCC_FILING_CARD",
    "ADMIN_PASSWORD",
    "GMAIL_USER",
    "GMAIL_APP_PASSWORD",
]

_PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "ai-biz-launcher")
_cache: dict[str, str] = {}

try:
    from google.cloud import secretmanager
    _client = secretmanager.SecretManagerServiceClient()
except Exception as e:
    _client = None
    print(f"⚠️ Secret Manager client unavailable ({e}) - all secrets will fall back to .env.")

def get_secret(name: str) -> str:
    """Returns the value for `name`, preferring Secret Manager's "latest"
    version and falling back to os.getenv(name) if the client isn't
    configured or the call fails for any reason (missing secret, no
    permissions, offline, etc) - a broken Secret Manager lookup should
    degrade to local dev behavior, not crash the app."""
    if name in _cache:
        return _cache[name]

    value = None
    if _client is not None:
        try:
            resource_name = f"projects/{_PROJECT_ID}/secrets/{name}/versions/latest"
            response = _client.access_secret_version(name=resource_name)
            value = response.payload.data.decode("UTF-8")
        except Exception as e:
            print(f"⚠️ Could not fetch secret '{name}' from Secret Manager ({e}) - falling back to .env.")

    if value is None:
        value = os.getenv(name, "")

    _cache[name] = value
    return value

def preload():
    """Warms the cache for every managed secret. Called once at app
    startup so the first real request never pays the Secret Manager
    round-trip - and so a misconfigured secret shows up in startup logs
    instead of mid-request."""
    for name in MANAGED_SECRETS:
        get_secret(name)
