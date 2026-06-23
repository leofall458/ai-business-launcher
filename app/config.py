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
