import json
import os
from dotenv import load_dotenv
from app.secrets import get_secret

load_dotenv()

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "ai-biz-launcher")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
MODEL = "gemini-2.5-flash"

STRIPE_SECRET_KEY = get_secret("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = get_secret("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = get_secret("STRIPE_WEBHOOK_SECRET")

LLC_FORMATION_PRICE_CENTS = 35000

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "ai-biz-launcher")

GITHUB_TOKEN = get_secret("GITHUB_TOKEN")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Card used to pay the $100 VA SCC state filing fee on each LLC order. Stored
# as a single JSON-encoded secret in Secret Manager (never in source) since
# it's a set of related fields, not one token - see app/secrets.py.
_scc_filing_card_raw = get_secret("SCC_FILING_CARD")
SCC_FILING_CARD = json.loads(_scc_filing_card_raw) if _scc_filing_card_raw else {}
