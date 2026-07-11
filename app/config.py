import json
import os
from dotenv import load_dotenv
from app.secrets import get_secret

load_dotenv()

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "ai-biz-launcher")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
MODEL = "gemini-2.5-flash"

# "staging" picks up the _STAGING-suffixed Secret Manager entries (test-mode
# Stripe keys, a separate webhook signing secret) instead of the production
# ones - same project, same secrets list, just a different version of each
# so a staging deploy can never accidentally touch live Stripe.
APP_ENV = os.getenv("APP_ENV", "production")
_secret_suffix = "_STAGING" if APP_ENV == "staging" else ""

STRIPE_SECRET_KEY = get_secret(f"STRIPE_SECRET_KEY{_secret_suffix}")
STRIPE_PUBLISHABLE_KEY = get_secret(f"STRIPE_PUBLISHABLE_KEY{_secret_suffix}")
STRIPE_WEBHOOK_SECRET = get_secret(f"STRIPE_WEBHOOK_SECRET{_secret_suffix}")

LLC_FORMATION_PRICE_CENTS = 35000

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "ai-biz-launcher")

# Staging writes to its own Firestore collection (e.g. "staging_orders") in
# the same project/database, so test orders can never mix with real ones -
# no separate Firestore instance needed just for that isolation.
ORDERS_COLLECTION = os.getenv("ORDERS_COLLECTION", "orders")

# Same isolation, derived from APP_ENV directly rather than needing its own
# Cloud Run env var like ORDERS_COLLECTION does - page view counts have no
# reason to need per-deploy overriding, and this way staging load/E2E
# testing traffic can never inflate the production visit count the admin
# dashboard shows.
PAGE_VIEWS_COLLECTION = "staging_page_views" if APP_ENV == "staging" else "page_views"

GITHUB_TOKEN = get_secret("GITHUB_TOKEN")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "")

ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD")

# Card used to pay the $100 VA SCC state filing fee on each LLC order. Stored
# as a single JSON-encoded secret in Secret Manager (never in source) since
# it's a set of related fields, not one token - see app/secrets.py.
_scc_filing_card_raw = get_secret("SCC_FILING_CARD")
SCC_FILING_CARD = json.loads(_scc_filing_card_raw) if _scc_filing_card_raw else {}

GMAIL_USER = get_secret("GMAIL_USER")
GMAIL_APP_PASSWORD = get_secret("GMAIL_APP_PASSWORD")
SUPPORT_EMAIL = "support@launchbridge.ai"

# Email-to-SMS gateway address for the admin's phone (e.g. a tmomail.net
# address for T-Mobile) - see app/sms.py.
ADMIN_PHONE_EMAIL = get_secret("ADMIN_PHONE_EMAIL")

# Signs magic links, dashboard sessions, CSRF tokens, and the admin
# EIN-filing-link token (see app/dashboard_auth.py, app/dashboard_security.py,
# app/main.py) - a pure server-side HMAC key with no external meaning,
# generated and stored directly in Secret Manager rather than something
# the user provides.
STATUS_SESSION_SECRET = get_secret("STATUS_SESSION_SECRET")

# Private, CMEK-encrypted GCS bucket holding LLC certificates and similar
# order documents - never served as a public URL; always reached through
# a short-lived V4 signed URL (see app/document_store.py and
# /orders/{order_id}/documents/{doc_id} in app/main.py).
STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "ai-biz-launcher-llc-certificates")

# Optional: enables Google Places address autocomplete on the dashboard intake form.
# Set GOOGLE_PLACES_API_KEY in .env (or Cloud Run env vars). Leave empty to disable.
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")

# Optional: GA4 measurement ID (e.g. "G-XXXXXXXXXX"). Leave empty to disable tracking.
GOOGLE_ANALYTICS_ID = os.getenv("GOOGLE_ANALYTICS_ID", "")

# Optional: Microsoft Clarity project ID. Leave empty to disable session recording.
CLARITY_ID = os.getenv("CLARITY_ID", "")

# Optional: URL of a deployed sample site for the deliverables gallery iframe preview.
SAMPLE_WEBSITE_URL = os.getenv("SAMPLE_WEBSITE_URL", "")

# Google Ads offline conversion import (see app/google_ads_service.py) -
# reports a completed Stripe payment back to Google Ads against its gclid,
# server-side, from the Stripe webhook. All optional: any of these being
# empty just disables the import (skipped, not an error - see
# google_ads_service.upload_click_conversion). Not split into _STAGING
# variants like the Stripe keys - the service only ever calls the live API
# when APP_ENV == "production" in the first place, so there's nothing for a
# staging-only credential to protect against.
GOOGLE_ADS_DEVELOPER_TOKEN = get_secret("GOOGLE_ADS_DEVELOPER_TOKEN")
GOOGLE_ADS_CLIENT_ID = get_secret("GOOGLE_ADS_CLIENT_ID")
GOOGLE_ADS_CLIENT_SECRET = get_secret("GOOGLE_ADS_CLIENT_SECRET")
GOOGLE_ADS_REFRESH_TOKEN = get_secret("GOOGLE_ADS_REFRESH_TOKEN")
# Only needed if GOOGLE_ADS_CUSTOMER_ID is managed under a manager (MCC)
# account rather than being logged into directly.
GOOGLE_ADS_LOGIN_CUSTOMER_ID = get_secret("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
# Target account ID that owns the conversion action, digits only (no dashes).
GOOGLE_ADS_CUSTOMER_ID = get_secret("GOOGLE_ADS_CUSTOMER_ID")
# Full resource name, e.g. "customers/1234567890/conversionActions/567890123".
GOOGLE_ADS_CONVERSION_ACTION = get_secret("GOOGLE_ADS_CONVERSION_ACTION")
