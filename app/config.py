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

FOUNDING_MEMBER_DISCOUNT = True
FOUNDING_MEMBER_MAX = 10
FOUNDING_MEMBER_PRICE_CENTS = 20000          # $200 total (43% off $350)
FOUNDING_MEMBER_SERVICE_FEE_CENTS = 10000   # $100 service fee (normally $250)
FOUNDING_MEMBER_DISCOUNT_PERCENT = 43
FOUNDING_MEMBER_LABEL = "Founding Member"

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "ai-biz-launcher")

# Staging writes to its own Firestore collection (e.g. "staging_orders") in
# the same project/database, so test orders can never mix with real ones -
# no separate Firestore instance needed just for that isolation.
ORDERS_COLLECTION = os.getenv("ORDERS_COLLECTION", "orders")

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

# Optional: URL of a deployed sample site for the deliverables gallery iframe preview.
SAMPLE_WEBSITE_URL = os.getenv("SAMPLE_WEBSITE_URL", "")
