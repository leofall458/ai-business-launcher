# Launch Bridge — Codebase Summary

Generated 2026-06-26. Reference document for AI-assisted development.

---

## 1. Complete File Tree

```
ai-business-launcher/
├── Dockerfile
├── README.md
├── CODEBASE_SUMMARY.md
├── _test_launch.py
├── _tmp_check_ein_stuck.py
├── apphosting.yaml
├── check_scc.py
├── check_scc_llc_form.py
├── check_scc_namesearch.py
├── check_scc_result.py
├── cloudbuild.yaml
├── find_name_check_btn.py
├── firebase.json
├── firestore.indexes.json
├── firestore.rules
├── gmail_credentials.json          ← OAuth2 client (local only, not in git)
├── gmail_token.json                ← OAuth2 token cache (local only, not in git)
├── keep_scc_alive.py
├── launch_bridge_admin.py          ← Local desktop admin app (Tkinter)
├── pyproject.toml
├── save_scc_session.py
├── scc_filer.py
├── start_launchbridge.bat
├── test_gemini.py
├── test_playwright.py
├── uv.lock
│
├── app/
│   ├── __init__.py
│   ├── check_scc_status.py
│   ├── config.py
│   ├── dashboard_auth.py
│   ├── dashboard_security.py
│   ├── deployer.py
│   ├── document_store.py
│   ├── ein_filer.py
│   ├── email_service.py
│   ├── gmail_poller.py             ← Local only (needs Gmail OAuth2)
│   ├── local_filing_poller.py
│   ├── log_scrub.py
│   ├── main.py                     ← FastAPI app (2205 lines)
│   ├── notify.py
│   ├── photo_utils.py
│   ├── scc_llc_filer.py
│   ├── secrets.py
│   ├── sms.py
│   ├── ssn_vault.py
│   ├── stripe_service.py
│   ├── validators.py
│   │
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── brand_agent.py
│   │   ├── brand_pdf.py
│   │   ├── ein_agent.py
│   │   ├── llc_agent.py
│   │   ├── marketing_agent.py
│   │   ├── name_agent.py
│   │   ├── name_check_agent.py
│   │   ├── pdf_agent.py
│   │   ├── scc_name_check.py
│   │   └── website_agent.py
│   │
│   ├── routes/
│   │   └── __init__.py
│   │
│   ├── static/
│   │   ├── docs/                   ← Sample LLC packages (static PDFs)
│   │   │   ├── Executive_Paws_LLC_LLC_Package.pdf
│   │   │   ├── Golden_Kernel_Popcorn_Co_LLC_LLC_Package.pdf
│   │   │   ├── Kiln_&_Clay_Pottery_Studio_LLC_LLC_Package.pdf
│   │   │   ├── Paws_LLC_LLC_Package.pdf
│   │   │   └── Spokewise_Mobile_Bike_Repair_LLC_LLC_Package.pdf
│   │   ├── examples/
│   │   │   ├── brand_kit.svg
│   │   │   ├── demo_site.html
│   │   │   └── doc_mockup.svg
│   │   └── preview/
│   │
│   ├── templates/
│   │   ├── _ga4.html               ← GA4 snippet partial
│   │   ├── _wizard.html            ← Step 1 + Step 2 partial (included on all conversion pages)
│   │   ├── admin.html
│   │   ├── contact_result.html
│   │   ├── contact_success.html
│   │   ├── dashboard_check_email.html
│   │   ├── dashboard_login.html
│   │   ├── dashboard_order.html    ← Post-payment intake + customer status
│   │   ├── dashboard_order_list.html
│   │   ├── examples.html
│   │   ├── existing_llc_verify_result.html
│   │   ├── form_errors.html
│   │   ├── index.html              ← Homepage
│   │   ├── name_check_result.html
│   │   ├── privacy.html
│   │   ├── result.html             ← Name screening result (htmx partial)
│   │   ├── status_timeline.html    ← Timeline partial (htmx auto-refresh)
│   │   ├── success_interstitial.html ← Post-payment "check your email" page
│   │   ├── terms.html
│   │   ├── virginia_llc_contractors.html
│   │   ├── virginia_llc_done_for_you.html
│   │   ├── virginia_llc_pricing.html
│   │   └── websites/               ← Customer website Jinja2 templates
│   │       ├── local.html
│   │       ├── modern.html
│   │       └── professional.html
│   │
│   └── utils/
│       ├── __init__.py
│       └── irs_hours.py
```

**Note:** `app/templates/status.html` and `app/templates/launch_result.html` do not exist. The user dashboard uses `dashboard_order.html`; the timeline partial is `status_timeline.html`; post-payment interstitial is `success_interstitial.html`.

---

## 2. Full Contents of Key Files

### app/config.py

```python
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
```

---

### app/templates/index.html

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Launch Bridge — AI-Powered Virginia LLC Formation</title>
    <meta name="description" content="Form a Virginia LLC in days with AI. Get your Articles, EIN, brand kit, business website, and Stripe account — all for $350 flat.">
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @keyframes pulse-green { 0%,100%{opacity:1} 50%{opacity:.4} }
        .live-dot { animation: pulse-green 2s infinite; }
    </style>
    {% include "_ga4.html" %}
</head>
<body class="bg-gray-950 text-white min-h-screen">

<div class="max-w-xl mx-auto px-4">

    <!-- ── HEADER ── -->
    <header class="text-center pt-10 pb-6">
        <div class="inline-flex items-center gap-2 text-xs font-semibold tracking-widest uppercase text-blue-400 mb-4">
            <span class="live-dot w-2 h-2 bg-blue-400 rounded-full inline-block"></span>
            Virginia LLC Formation
        </div>
        <h1 class="text-4xl sm:text-5xl font-black tracking-tight mb-3">Launch Bridge</h1>
        <p class="text-gray-400 text-base sm:text-lg mb-5 max-w-sm mx-auto">
            From business idea to fully-formed LLC with a website, brand kit, EIN, and Stripe account — all done for you.
        </p>
        <div class="inline-flex items-center gap-2 bg-green-900/40 border border-green-800/60 rounded-full px-5 py-2 text-green-400 text-sm font-bold">
            $350 flat · includes $100 Virginia state fee
        </div>
    </header>

    {% if cancelled %}
    <div class="bg-yellow-950 border border-yellow-700 rounded-xl p-4 mb-6 text-center text-sm text-yellow-300">
        Payment cancelled — no charge was made. Fill out the form below whenever you're ready.
    </div>
    {% endif %}

    <!-- Already have an order? -->
    <a href="/dashboard"
        class="flex items-center justify-between bg-gray-900 border border-gray-700 hover:border-blue-600 rounded-xl p-4 mb-8 text-sm font-semibold text-blue-400 transition">
        <span>📦 Already have an order?</span>
        <span>Sign in to your dashboard →</span>
    </a>

    <!-- ── DELIVERABLES GALLERY ── -->
    <section class="mb-10">
        <div class="flex items-end justify-between mb-5">
            <div>
                <h2 class="text-xl font-bold">Here's exactly what you get</h2>
                <p class="text-gray-500 text-xs mt-1">Real examples — built for every order</p>
            </div>
            <a href="/examples" class="text-blue-400 hover:text-blue-300 text-xs font-semibold transition shrink-0 ml-4">See all →</a>
        </div>

        <!-- Website browser preview -->
        <a href="/examples/demo-site" target="_blank" class="block group mb-3">
            <div class="rounded-2xl overflow-hidden shadow-2xl shadow-blue-950/50 ring-1 ring-gray-700 group-hover:ring-blue-600 transition-all duration-200">
                <div class="bg-gray-800 px-3 py-2.5 flex items-center gap-2.5 border-b border-gray-700/80">
                    <div class="flex gap-1.5 shrink-0">
                        <div class="w-3 h-3 rounded-full bg-red-500"></div>
                        <div class="w-3 h-3 rounded-full bg-yellow-400"></div>
                        <div class="w-3 h-3 rounded-full bg-green-500"></div>
                    </div>
                    <div class="flex-1 bg-gray-700/80 rounded px-2.5 py-1 text-xs text-gray-300 flex items-center gap-1.5 min-w-0">
                        <svg class="w-3 h-3 text-green-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/>
                        </svg>
                        <span class="truncate">blueridgeconsulting.launchbridge-sites.github.io</span>
                    </div>
                    <svg class="w-3.5 h-3.5 text-gray-500 group-hover:text-blue-400 shrink-0 transition" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/>
                    </svg>
                </div>
                <div style="height: 420px; overflow: hidden; position: relative; background: #fff;">
                    <iframe src="/examples/demo-site" style="width: 100%; height: 100%; border: none; display: block;" scrolling="yes" title="Blue Ridge Consulting LLC — example website"></iframe>
                    <div class="absolute inset-0" style="cursor: pointer;" title="Open full site"></div>
                </div>
            </div>
        </a>

        <div class="flex items-center justify-between mb-5 px-1">
            <p class="text-xs text-gray-500">🌐 A real example of the website we build for every customer</p>
            <a href="/examples/demo-site" target="_blank" class="text-blue-400 hover:text-blue-300 text-xs font-semibold transition shrink-0 ml-3">View full site →</a>
        </div>

        <div class="grid grid-cols-3 gap-2">
            <a href="/examples#brand-kit" class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden hover:border-purple-700 transition block">
                <div class="h-24 overflow-hidden bg-gray-800">
                    <img src="/static/examples/brand_kit.svg" alt="Brand kit" class="w-full h-full" style="object-fit:cover; object-position: top left;"/>
                </div>
                <div class="p-2.5">
                    <p class="text-white font-semibold text-xs">🎨 Brand Kit</p>
                    <p class="text-gray-500 text-[10px] mt-0.5 leading-snug">Logo · colors · fonts · tagline</p>
                </div>
            </a>
            <a href="/examples#documents" class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden hover:border-green-700 transition block">
                <div class="h-24 bg-gray-800 flex items-center justify-center overflow-hidden p-1">
                    <img src="/static/examples/doc_mockup.svg" alt="LLC documents" class="h-full w-auto"/>
                </div>
                <div class="p-2.5">
                    <p class="text-white font-semibold text-xs">📄 LLC Docs</p>
                    <p class="text-gray-500 text-[10px] mt-0.5 leading-snug">Articles · Op. Agmt · EIN</p>
                </div>
            </a>
            <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
                <div class="h-24 bg-gray-800 p-2 flex flex-col justify-center gap-1.5">
                    <div class="bg-indigo-900/60 border border-indigo-700/40 rounded px-2 py-1.5 flex items-center gap-1.5">
                        <div class="text-indigo-400 font-black text-xs">S</div>
                        <div class="flex-1">
                            <div class="h-1 bg-indigo-400/50 rounded w-full mb-1"></div>
                            <div class="h-1 bg-indigo-300/30 rounded w-2/3"></div>
                        </div>
                        <div class="text-green-400 text-[9px] font-bold">Live</div>
                    </div>
                    <div class="flex gap-1">
                        <div class="h-4 flex-1 bg-gray-700 rounded flex items-center justify-center text-[8px] text-gray-400">Visa</div>
                        <div class="h-4 flex-1 bg-gray-700 rounded flex items-center justify-center text-[8px] text-gray-400">⌘ Pay</div>
                        <div class="h-4 flex-1 bg-gray-700 rounded flex items-center justify-center text-[8px] text-gray-400">G Pay</div>
                    </div>
                </div>
                <div class="p-2.5">
                    <p class="text-white font-semibold text-xs">💳 Stripe</p>
                    <p class="text-gray-500 text-[10px] mt-0.5 leading-snug">Accept payments day 1</p>
                </div>
            </div>
        </div>

        <div class="bg-gray-900/50 border border-gray-800 rounded-xl p-4 mt-4">
            <div class="flex justify-between text-sm text-gray-300 mb-2">
                <span>Launch Bridge service fee</span><span class="text-white font-semibold">$250</span>
            </div>
            <div class="flex justify-between text-sm text-gray-300 mb-3">
                <span>Virginia state filing fee</span><span class="text-white font-semibold">$100</span>
            </div>
            <div class="flex justify-between font-bold border-t border-gray-700 pt-3">
                <span class="text-white">Total</span><span class="text-green-400 text-lg">$350</span>
            </div>
            <p class="text-xs text-gray-600 mt-2">We pay Virginia directly — you never pay separately.</p>
        </div>
    </section>

    {% include "_wizard.html" %}

    <footer class="text-center py-10 border-t border-gray-900 mt-8">
        <p class="text-xs text-gray-600">
            © 2026 Launch Bridge LLC ·
            <a href="/terms" class="text-gray-500 hover:text-gray-400">Terms</a> ·
            <a href="/privacy" class="text-gray-500 hover:text-gray-400">Privacy</a> ·
            <a href="mailto:support@launchbridge.ai" class="text-gray-500 hover:text-gray-400">support@launchbridge.ai</a>
        </p>
    </footer>

</div>
</body>
</html>
```

---

### app/agents/website_agent.py

```python
import os
import json
from jinja2 import Environment, FileSystemLoader
from google.genai import types
from app.agents import get_client

MODEL = "gemini-2.5-flash"

WEBSITES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates", "websites")
TEMPLATE_FILES = {
    "professional": "professional.html",
    "local": "local.html",
    "modern": "modern.html",
}

TEMPLATE_DEFAULT_COLORS = {
    "professional": ("#1E3A5F", "#64748B"),
    "local": ("#9A3412", "#F59E0B"),
    "modern": ("#22D3EE", "#A855F7"),
}

_jinja_env = Environment(loader=FileSystemLoader(WEBSITES_DIR))

CONTENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "tagline": {"type": "STRING"},
        "about_text": {"type": "STRING"},
        "services": {
            "type": "ARRAY", "minItems": 3, "maxItems": 3,
            "items": {"type": "OBJECT", "properties": {
                "name": {"type": "STRING"}, "description": {"type": "STRING"}
            }, "required": ["name", "description"]}
        },
        "cta_text": {"type": "STRING"},
        "faq": {
            "type": "ARRAY", "minItems": 4, "maxItems": 5,
            "items": {"type": "OBJECT", "properties": {
                "question": {"type": "STRING"}, "answer": {"type": "STRING"}
            }, "required": ["question", "answer"]}
        },
        "primary_color": {"type": "STRING", "pattern": "^#[0-9A-Fa-f]{6}$"},
        "secondary_color": {"type": "STRING", "pattern": "^#[0-9A-Fa-f]{6}$"},
    },
    "required": ["tagline", "about_text", "services", "cta_text", "faq", "primary_color", "secondary_color"]
}

def generate_website_content(business_name, business_idea, target_customer):
    client = get_client()
    prompt = f"""You are a copywriter creating website content for a new Virginia small business.
Business Name: {business_name}
Business Idea: {business_idea}
Target Customer: {target_customer}
Write a tagline, about paragraph, 3 services, CTA button label, 4-5 FAQs, and a primary/secondary hex color pair."""
    response = client.models.generate_content(
        model=MODEL, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=CONTENT_SCHEMA),
    )
    return json.loads(response.text)

def _merge_services(customer_services, ai_services):
    merged = []
    for i in range(3):
        customer = customer_services[i] if i < len(customer_services) else {}
        ai = ai_services[i] if ai_services and i < len(ai_services) else {"name": "", "description": ""}
        name = (customer.get("name") or "").strip() or ai.get("name", "")
        description = (customer.get("description") or "").strip() or ai.get("description", "")
        merged.append({"name": name, "description": description})
    return merged

def render_website_html(content, business_name, email, phone, address, template_name,
                         payment_link_url=None, hero_photo=None, gallery_photos=None,
                         hours=None, instagram_url=None, facebook_url=None, tiktok_url=None, order_id=None):
    if template_name not in TEMPLATE_FILES:
        template_name = "professional"
    template = _jinja_env.get_template(TEMPLATE_FILES[template_name])
    return template.render(
        business_name=business_name, tagline=content["tagline"], about_text=content["about_text"],
        services=content["services"], cta_text=content.get("cta_text") or "Get in Touch",
        faq=content.get("faq") or [], primary_color=content["primary_color"],
        secondary_color=content["secondary_color"], email=email, phone=phone, address=address,
        payment_link_url=payment_link_url, hero_photo=hero_photo, gallery_photos=gallery_photos or [],
        hours=hours, instagram_url=instagram_url, facebook_url=facebook_url, tiktok_url=tiktok_url,
        order_id=order_id, contact_endpoint="https://app.launchbridge.ai/contact",
    )

def generate_website(business_name, business_idea, target_customer, email, phone, address,
                     template_name="professional", tagline=None, description=None, services=None,
                     hours=None, photos=None, instagram_url=None, facebook_url=None, tiktok_url=None,
                     color_preference="default", custom_primary_color=None, payment_link_url=None, order_id=None):
    if template_name not in TEMPLATE_FILES:
        template_name = "professional"
    services = services or [{}, {}, {}]
    tagline = (tagline or "").strip()
    description = (description or "").strip()

    ai_content = generate_website_content(business_name, business_idea, target_customer)
    final_tagline = tagline or ai_content["tagline"]
    final_about = description or ai_content["about_text"]
    final_services = _merge_services(services, ai_content["services"])

    if color_preference == "brand_kit":
        primary_color, secondary_color = ai_content["primary_color"], ai_content["secondary_color"]
    elif color_preference == "custom" and custom_primary_color:
        _, default_secondary = TEMPLATE_DEFAULT_COLORS[template_name]
        primary_color, secondary_color = custom_primary_color, default_secondary
    else:
        primary_color, secondary_color = TEMPLATE_DEFAULT_COLORS[template_name]

    content = {
        "tagline": final_tagline, "about_text": final_about, "services": final_services,
        "cta_text": ai_content["cta_text"], "faq": ai_content["faq"],
        "primary_color": primary_color, "secondary_color": secondary_color,
    }

    photos = [p for p in (photos or []) if p]
    html = render_website_html(
        content, business_name, email, phone, address, template_name=template_name,
        payment_link_url=payment_link_url, hero_photo=photos[0] if photos else None,
        gallery_photos=photos, hours=(hours or "").strip() or None,
        instagram_url=(instagram_url or "").strip() or None,
        facebook_url=(facebook_url or "").strip() or None,
        tiktok_url=(tiktok_url or "").strip() or None, order_id=order_id,
    )
    return {"html": html, "template": template_name, "content": content}
```

---

### app/agents/brand_agent.py

```python
import json
from google.genai import types
from app.agents import get_client
from app.agents.brand_pdf import build_brand_kit_pdf

MODEL = "gemini-2.5-flash"

BRAND_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "colors": {
            "type": "OBJECT",
            "properties": {
                "primary": {"type": "OBJECT", "properties": {"hex": {"type": "STRING"}, "name": {"type": "STRING"}}, "required": ["hex", "name"]},
                "secondary": {"type": "OBJECT", "properties": {"hex": {"type": "STRING"}, "name": {"type": "STRING"}}, "required": ["hex", "name"]},
                "background": {"type": "OBJECT", "properties": {"hex": {"type": "STRING"}, "name": {"type": "STRING"}}, "required": ["hex", "name"]},
                "text": {"type": "OBJECT", "properties": {"hex": {"type": "STRING"}, "name": {"type": "STRING"}}, "required": ["hex", "name"]},
                "light": {"type": "OBJECT", "properties": {"hex": {"type": "STRING"}, "name": {"type": "STRING"}}, "required": ["hex", "name"]},
            },
            "required": ["primary", "secondary", "background", "text", "light"],
        },
        "fonts": {
            "type": "OBJECT",
            "properties": {
                "heading": {"type": "STRING"},
                "body": {"type": "STRING"},
                "pairing_rationale": {"type": "STRING"},
            },
            "required": ["heading", "body", "pairing_rationale"],
        },
        "taglines": {"type": "ARRAY", "minItems": 3, "maxItems": 3, "items": {"type": "STRING"}},
        "voice": {
            "type": "OBJECT",
            "properties": {
                "tone": {"type": "STRING"},
                "values": {"type": "ARRAY", "minItems": 3, "maxItems": 3, "items": {"type": "STRING"}},
                "audience": {"type": "STRING"},
                "story": {"type": "STRING"},
            },
            "required": ["tone", "values", "audience", "story"],
        },
        "social": {
            "type": "OBJECT",
            "properties": {
                "photo_style": {"type": "STRING"},
                "post_templates": {"type": "ARRAY", "minItems": 3, "maxItems": 3, "items": {"type": "STRING"}},
                "hashtags": {"type": "ARRAY", "minItems": 5, "maxItems": 8, "items": {"type": "STRING"}},
            },
            "required": ["photo_style", "post_templates", "hashtags"],
        },
    },
    "required": ["colors", "fonts", "taglines", "voice", "social"],
}

def _generate_brand_data(business_name, business_idea, target_customer):
    client = get_client()
    prompt = f"""You are a brand designer creating a complete brand identity for a new Virginia small business.
Business Name: {business_name}
Business Idea: {business_idea}
Target Customer: {target_customer}
Design a 5-color palette, heading/body Google Font pairing, 3 taglines, brand voice, and social media kit."""
    response = client.models.generate_content(
        model=MODEL, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=BRAND_SCHEMA),
    )
    return json.loads(response.text)

def _generate_logo_svg(business_name, business_idea, primary_hex, secondary_hex):
    """Returns "" on failure - callers fall back to a vector monogram."""
    try:
        client = get_client()
        prompt = f"""Design a simple, professional SVG logo for:
Business Name: {business_name}
Business Idea: {business_idea}
Use colors {primary_hex} and {secondary_hex}. viewBox="0 0 200 200", no external resources.
Respond with ONLY the raw <svg>...</svg> markup."""
        response = client.models.generate_content(model=MODEL, contents=prompt)
        svg = (response.text or "").strip()
        if svg.startswith("```"):
            svg = svg.split("\n", 1)[1] if "\n" in svg else ""
            if svg.endswith("```"):
                svg = svg.rsplit("```", 1)[0]
        svg = svg.strip()
        if svg.startswith("<svg") and svg.endswith("</svg>"):
            return svg
        return ""
    except Exception:
        return ""

def generate_brand_kit(business_name, business_idea, target_customer,
                        full_name="", email="", phone="", website_url=""):
    brand = _generate_brand_data(business_name, business_idea, target_customer)
    primary_hex = brand["colors"]["primary"]["hex"]
    secondary_hex = brand["colors"]["secondary"]["hex"]
    logo_svg = _generate_logo_svg(business_name, business_idea, primary_hex, secondary_hex)
    brand["logo_svg"] = logo_svg
    pdf_bytes = build_brand_kit_pdf(brand, business_name, full_name, email, phone, website_url)
    return {
        "status": "complete",
        "business_name": business_name,
        "colors": brand["colors"],
        "fonts": brand["fonts"],
        "taglines": brand["taglines"],
        "voice": brand["voice"],
        "social": brand["social"],
        "logo_svg": logo_svg,
        "pdf_bytes": pdf_bytes,  # caller must pop before saving to Firestore
    }
```

---

### app/stripe_service.py

```python
import stripe
from app.config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, LLC_FORMATION_PRICE_CENTS

stripe.api_key = STRIPE_SECRET_KEY

def construct_webhook_event(payload: bytes, sig_header: str):
    return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)

def create_checkout_session(order_id, business_name, success_url, cancel_url):
    return stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price_data": {
            "currency": "usd", "unit_amount": LLC_FORMATION_PRICE_CENTS,
            "product_data": {"name": f"Virginia LLC Formation - {business_name}",
                "description": "$250 Launch Bridge service fee + $100 Virginia state filing fee"},
        }, "quantity": 1}],
        success_url=success_url, cancel_url=cancel_url,
        client_reference_id=order_id, metadata={"order_id": order_id},
    )

def retrieve_checkout_session(session_id):
    return stripe.checkout.Session.retrieve(session_id)

def create_connect_account(email, business_name, multi_member):
    """Standard account (Accounts v2 controller shape). Customer owns it, platform gets API access."""
    return stripe.Account.create(
        controller={"stripe_dashboard": {"type": "full"}, "fees": {"payer": "account"}, "losses": {"payments": "stripe"}},
        country="US", email=email, business_type="company",
        company={"name": business_name, "structure": "multi_member_llc" if multi_member else "single_member_llc"},
    )

def create_account_link(account_id, refresh_url, return_url):
    """Links expire in minutes — always generate fresh at the time of use."""
    link = stripe.AccountLink.create(account=account_id, refresh_url=refresh_url, return_url=return_url, type="account_onboarding")
    return link.url

def is_account_active(account_id):
    try:
        return bool(stripe.Account.retrieve(account_id).charges_enabled)
    except Exception:
        return False

def create_pay_what_you_want_payment_link(connect_account_id, business_name):
    """Pay-what-you-want link on the customer's own connected account. Min $5."""
    product = stripe.Product.create(name=f"Payment to {business_name}", stripe_account=connect_account_id)
    price = stripe.Price.create(
        currency="usd", custom_unit_amount={"enabled": True, "minimum": 500},
        product=product.id, stripe_account=connect_account_id,
    )
    return stripe.PaymentLink.create(line_items=[{"price": price.id, "quantity": 1}], stripe_account=connect_account_id).url
```

---

### app/ssn_vault.py

```python
"""Encrypted-at-rest SSN storage in Firestore via Cloud KMS.
Only base64-encoded ciphertext (ssn_encrypted) ever written to Firestore.
KMS project: ai-biz-launcher, location: us-east4, keyring: launchbridge-vault, key: ssn-key."""

import base64
import datetime
from google.cloud import firestore, kms
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
    order = _ORDERS.document(order_id).get().to_dict() or {}
    ciphertext_b64 = order.get("ssn_encrypted")
    if not ciphertext_b64:
        return ""
    try:
        ciphertext = base64.b64decode(ciphertext_b64)
        response = _kms_client.decrypt(request={"name": _key_name, "ciphertext": ciphertext})
        return response.plaintext.decode("utf-8")
    except Exception:
        return ""

def delete_ssn(order_id: str) -> bool:
    try:
        order_ref = _ORDERS.document(order_id)
        order_ref.set({"ssn_encrypted": firestore.DELETE_FIELD, "ssn_collected_at": firestore.DELETE_FIELD}, merge=True)
        order_ref.collection("events").add({"event": "ssn_deleted", "at": firestore.SERVER_TIMESTAMP})
        return True
    except Exception:
        return False

def is_ssn_stored(order_id: str) -> bool:
    return bool((_ORDERS.document(order_id).get().to_dict() or {}).get("ssn_encrypted"))

def ssn_age_hours(order_id: str) -> float:
    collected_at = (_ORDERS.document(order_id).get().to_dict() or {}).get("ssn_collected_at")
    if not collected_at:
        return float("inf")
    return (datetime.datetime.now(datetime.timezone.utc) - collected_at).total_seconds() / 3600
```

---

### app/sms.py

```python
"""Admin SMS via email-to-SMS gateway (ADMIN_PHONE_EMAIL is a carrier gateway address).
No SMS API — uses the same Gmail SMTP credentials as email_service.py."""

import smtplib
from email.message import EmailMessage
from app.config import GMAIL_USER, GMAIL_APP_PASSWORD, ADMIN_PHONE_EMAIL

SMS_MAX_LENGTH = 160

def send_admin_sms(message: str) -> bool:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not ADMIN_PHONE_EMAIL:
        print(f"⚠️ Admin SMS not configured - skipping alert: {message}")
        return False
    if len(message) > SMS_MAX_LENGTH:
        message = message[:SMS_MAX_LENGTH - 1] + "…"
    msg = EmailMessage()
    msg["From"] = GMAIL_USER
    msg["To"] = ADMIN_PHONE_EMAIL
    msg["Subject"] = ""
    msg.set_content(message)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"⚠️ Could not send admin SMS '{message}': {e}")
        return False
```

---

### app/gmail_poller.py

```python
"""LOCAL ONLY. Polls leofall458@gmail.com every 5 minutes for SCC approval emails
from CIS@scc.virginia.gov. Matches to filing_submitted orders, forwards a copy
to the customer with the Certificate of Organization PDF attached, uploads the PDF
to GCS, and advances the order past filing_confirmed.

Needs Gmail OAuth2: gmail_credentials.json + gmail_token.json in project root.
Run with: python3 -m app.gmail_poller
"""

import base64, os, re, time, traceback
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.cloud import firestore
from app.main import ORDERS, db, advance_past_filing_confirmed, run_asset_generation
from app.email_service import forward_scc_approval_email
from app.document_store import upload_document

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = "gmail_credentials.json"
TOKEN_FILE = "gmail_token.json"
SCC_SENDER = "CIS@scc.virginia.gov"
POLL_INTERVAL_SECONDS = 300
POLLER_STATUS_DOC = db.collection("system").document("gmail_poller")
PROCESSED_EMAILS = db.collection("processed_scc_emails")
CONFIRMATION_NUMBER_RE = re.compile(r"\b(\d{15})\b")

def get_gmail_service():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES) if os.path.exists(TOKEN_FILE) else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)

def _normalize(name):
    return re.sub(r"[^A-Z0-9]", "", name.upper())

def find_matching_order(subject, body):
    haystack = _normalize(f"{subject} {body}")
    for doc in ORDERS.where("state", "==", "filing_submitted").stream():
        order = doc.to_dict()
        business_name = order.get("business_name", "")
        if business_name and _normalize(business_name) in haystack:
            return doc.id, order
    return None, None

def process_message(service, message_id):
    if PROCESSED_EMAILS.document(message_id).get().exists:
        return
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    subject = next((h["value"] for h in headers if h.get("name","").lower()=="subject"), "")
    body_text = ""  # (extracted via _extract_body_text helper)
    order_id, order = find_matching_order(subject, body_text)
    if not order_id:
        PROCESSED_EMAILS.document(message_id).set({"subject": subject, "processed_at": firestore.SERVER_TIMESTAMP, "matched": False})
        return
    order_ref = ORDERS.document(order_id)
    # uploads certificate PDF, extracts confirmation number, forwards email to customer
    trigger_assets = advance_past_filing_confirmed(order_ref, order)
    if trigger_assets:
        run_asset_generation(order_id)
    PROCESSED_EMAILS.document(message_id).set({"matched": True, "order_id": order_id, "processed_at": firestore.SERVER_TIMESTAMP})

def main():
    service = get_gmail_service()
    while True:
        try:
            results = service.users().messages().list(userId="me", q=f"from:{SCC_SENDER}", maxResults=20).execute()
            POLLER_STATUS_DOC.set({"last_checked_at": firestore.SERVER_TIMESTAMP, "status": "ok"}, merge=True)
            for m in results.get("messages", []):
                process_message(service, m["id"])
        except Exception as e:
            POLLER_STATUS_DOC.set({"last_checked_at": firestore.SERVER_TIMESTAMP, "status": "error", "last_error": str(e)}, merge=True)
            traceback.print_exc()
        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
```

---

### app/scc_llc_filer.py

*(See full source at `app/scc_llc_filer.py` — 528 lines. Summary below.)*

Playwright-based automation that files a Virginia LLC on SCC's CIS portal.
- Connects to a local Chrome via CDP at `http://172.27.176.1:9222`
- Credentials: `SCC_USERNAME`, `SCC_PASSWORD` secrets
- Entry point: `file_llc_on_scc(customer_data, interactive=True)`
- Steps: login → entity type → LLC/Articles of Organization → name check → entity info → registered agent → principal address → signatures → review/payment
- Two RA paths: "launchbridge" (search for existing Leo Fall agent) or "self" (create new individual RA via modal, JS-injected fields)
- Payment: `pay_scc_filing_fee()` walks the SCC shopping cart → LexisNexis checkout using `SCC_FILING_CARD` (JSON secret)
- `interactive=False` in production; `interactive=True` for manual runs
- Saves screenshots to `/tmp/scc_*.png` on failure

```python
from playwright.sync_api import sync_playwright
from app.secrets import get_secret
from app.config import SCC_FILING_CARD

CDP_URL = "http://172.27.176.1:9222"

def file_llc_on_scc(customer_data: dict, interactive=True) -> bool:
    # customer_data keys: business_name, first_name, middle_name, last_name,
    # email, phone, address, city, zipcode, industry_code, duration,
    # registered_agent_choice ("launchbridge" or "self")
    ...
```

---

### app/ein_filer.py

*(See full source at `app/ein_filer.py` — 366 lines. Summary below.)*

Playwright-based automation that files EIN applications on the IRS Online EIN Assistant.
- Connects to local Chrome via CDP at `http://172.27.176.1:9222`
- Entry point: `file_ein_with_irs(customer_data, interactive=True, on_submitted=None)`
- Returns a result dict (never a bare bool); see docstring for all return cases
- Steps: landing → legal structure → responsible party/SSN → addresses → LLC details → business activity → review/submit
- `on_submitted` callback fires the instant Submit is clicked (before confirmation page is read) — caller uses this to durably record `ein_submitted_to_irs=True` and delete the SSN from the vault
- `_capture_cp575()` downloads the IRS CP575 letter PDF; falls back to `_build_fallback_confirmation_pdf()` if unavailable
- `type_field()` used instead of `fill()` for fields with JS keystroke validation that ignores synthetic events

```python
def file_ein_with_irs(customer_data: dict, interactive=True, on_submitted=None) -> dict:
    # customer_data keys: business_name, first_name, middle_name, last_name,
    # ssn, address, city, state, zipcode, phone, county,
    # start_month, start_year, members, business_description
    # Returns: {"success": True, "ein": "XX-XXXXXXX", "cp575_bytes": bytes|None}
    #       or {"success": False, "rejected": True}
    #       or {"success": False, "submitted": True, "ein": None, "screenshot": path}
    #       or {"success": False, "submitted": False, "error": str}
    ...
```

---

### app/deployer.py

```python
import re, time, base64, secrets, requests
from google.cloud import firestore
from app.config import GITHUB_TOKEN, GITHUB_USERNAME, FIREBASE_PROJECT_ID, ORDERS_COLLECTION

GITHUB_API = "https://api.github.com"

def slugify_repo_name(business_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", business_name.lower()).strip("-")
    slug = slug[:80].strip("-") or "business"
    return f"{slug}-{secrets.token_hex(3)}"

def create_repo(repo_name: str) -> bool:
    resp = requests.post(f"{GITHUB_API}/user/repos", headers=_headers(),
        json={"name": repo_name, "private": False, "auto_init": True}, timeout=30)
    return resp.status_code == 201

def push_index_html(repo_name: str, html: str) -> bool:
    content_b64 = base64.b64encode(html.encode()).decode()
    for attempt in range(3):
        resp = requests.put(f"{GITHUB_API}/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html",
            headers=_headers(), json={"message": "Deploy website", "content": content_b64, "branch": "main"}, timeout=30)
        if resp.status_code in (200, 201):
            return True
        if resp.status_code == 404 and attempt < 2:
            time.sleep(1.5)
            continue
        return False
    return False

def enable_pages(repo_name: str) -> bool:
    resp = requests.post(f"{GITHUB_API}/repos/{GITHUB_USERNAME}/{repo_name}/pages",
        headers=_headers(), json={"source": {"branch": "main", "path": "/"}}, timeout=30)
    return resp.status_code in (201, 204)

def wait_for_pages_build(repo_name: str, timeout_seconds=90, poll_interval=5) -> bool:
    """Polls GitHub Pages status until 'built'. Retries once on errored builds."""
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
                    requests.post(builds_url, headers=_headers(), timeout=30)
                    retried = True
                else:
                    return False
        time.sleep(poll_interval)
    return False

def update_index_html(repo_name: str, html: str) -> bool:
    sha = get_index_html_sha(repo_name)
    if not sha:
        return False
    content_b64 = base64.b64encode(html.encode()).decode()
    resp = requests.put(f"{GITHUB_API}/repos/{GITHUB_USERNAME}/{repo_name}/contents/index.html",
        headers=_headers(), json={"message": "Update website", "content": content_b64, "branch": "main", "sha": sha}, timeout=30)
    return resp.status_code in (200, 201)

def deploy_website(business_name: str, html_content: str, order_id: str = None) -> dict:
    """Creates GitHub repo, pushes index.html, enables Pages, waits for build.
    Returns {"url": ..., "repo": ...} or None on failure.
    Website URL: https://{GITHUB_USERNAME}.github.io/{repo_name}"""
    repo_name = slugify_repo_name(business_name)
    if not create_repo(repo_name): return None
    if not push_index_html(repo_name, html_content): return None
    if not enable_pages(repo_name): return None
    if not wait_for_pages_build(repo_name): return None
    url = f"https://{GITHUB_USERNAME}.github.io/{repo_name}"
    if order_id:
        save_website_to_order(order_id, url, repo_name)
    return {"url": url, "repo": repo_name}
```

---

### launch_bridge_admin.py

*(See full source at `launch_bridge_admin.py` — 723 lines. Summary below.)*

Local desktop admin app (Tkinter + pystray). Run with `python launch_bridge_admin.py`.

**Startup sequence:**
1. Sets up Windows portproxy via PowerShell (UAC prompt, one-time) so WSL can reach Chrome's CDP port
2. Launches Chrome with `--remote-debugging-port=9222` and `--user-data-dir=C:\LaunchBridgeChromeDebugProfile`
3. Shows splash window prompting login to SCC; user clicks Continue
4. Saves SCC session cookies to `scc_session.json`
5. Starts keep-alive loop (pings SCC dashboard every 180s to keep session active)
6. Shows main dashboard window

**Main dashboard:**
- Status indicators: Chrome CDP reachability, SCC session state, IRS hours
- Pending orders list (polls Firestore every 30s)
- Buttons: Approve + File SCC, File EIN (IRS-hours-gated), View Status Page, Open Admin
- System tray icon with pending order count badge
- Minimize to tray on window close

**Key constants:**
```python
CDP_URL = "http://172.27.176.1:9222"   # WSL → Windows Chrome
SESSION_FILE = "scc_session.json"
APP_BASE_URL = "https://app.launchbridge.ai"
KEEP_ALIVE_INTERVAL_SECONDS = 180
ORDERS_POLL_INTERVAL_SECONDS = 30
```

---

### app/main.py

*(Full source: `app/main.py` — 2205 lines. Key sections below.)*

FastAPI application entry point. All business logic lives here.

**Startup:**
```python
@app.on_event("startup")
async def on_startup():
    preload_secrets()
    if os.getenv("ENABLE_EIN_SCHEDULER", "true").lower() == "true":
        asyncio.create_task(ein_queue_scheduler())   # Playwright, disabled on Cloud Run
    asyncio.create_task(ssn_expiry_scheduler())      # Safe everywhere
    asyncio.create_task(abandoned_cart_scheduler())  # Safe everywhere
```

**Order state machine constants:**
```python
ORDER_STATES = [
    "draft", "paid", "name_cleared", "review_approved", "filing_submitted",
    "filing_confirmed", "awaiting_ein_filing", "ein_requested", "ein_issued",
    "assets_generated", "complete",
]
```

**Key internal functions:**
- `record_state(order_ref, new_state, **extra_fields)` — writes state + appends to events subcollection
- `run_name_check(order_id)` — checks SCC availability, advances paid → name_cleared
- `run_document_generation(order_id)` — brand kit, marketing plan, name screening, LLC PDF
- `run_scc_filing(order_id)` — calls `file_llc_on_scc()`, advances to filing_submitted
- `run_ein_filing(order_id)` — calls `file_ein_with_irs()`, advances to ein_issued
- `run_asset_generation(order_id)` — Stripe Connect setup + website generation + deploy
- `advance_past_filing_confirmed(order_ref, order)` — handles skip_ein shortcut, queues EIN on IRS hours
- `process_paid_order(order_id, payment_status, background_tasks)` — idempotent; called by /success and /webhook
- `mark_ein_issued(order_ref, order, order_id, ein, background_tasks)` — shared finish line for EIN

---

## 3. All Routes

| Method | Path | Handler | Description |
|--------|------|---------|-------------|
| GET | `/` | `home` | Homepage with wizard |
| GET | `/examples` | `examples_page` | Examples gallery |
| GET | `/examples/demo-site` | `examples_demo_site` | Serve static demo site HTML |
| GET | `/virginia-llc/contractors` | `landing_contractors` | Niche landing page |
| GET | `/virginia-llc/done-for-you` | `landing_done_for_you` | Niche landing page |
| GET | `/virginia-llc/pricing` | `landing_pricing` | Pricing landing page |
| GET | `/privacy` | `privacy` | Privacy policy |
| GET | `/terms` | `terms` | Terms of service |
| POST | `/api/capture-lead` | `api_capture_lead` | Abandoned cart lead capture (Step 1/2 wizard) |
| POST | `/screen-name` | `screen_name` | htmx: Gemini business name screening |
| POST | `/check-name` | `check_name` | htmx: SCC + Gemini trademark name check |
| POST | `/verify-existing-llc` | `verify_existing_llc` | htmx: Verify existing LLC on SCC |
| POST | `/launch` | `launch` | Pre-payment form submit → Stripe Checkout redirect |
| GET | `/success` | `success` | Post-Stripe Checkout redirect; kicks off pipeline |
| POST | `/webhook` | `stripe_webhook` | Stripe webhook (server-side backup for /success) |
| GET | `/cancel` | `cancel` | Redirect to /?cancelled=1 |
| GET | `/connect/onboard/{order_id}` | `connect_onboard` | Stripe Connect onboarding link (auth: lb_session) |
| GET | `/dashboard` | `dashboard_login` | Magic link login page |
| POST | `/dashboard/login` | `dashboard_login_submit` | Send magic link email |
| GET | `/dashboard/verify` | `dashboard_verify` | Redeem magic link, set lb_session cookie |
| GET | `/dashboard/logout` | `dashboard_logout` | Clear lb_session |
| GET | `/dashboard/orders` | `dashboard_orders` | List customer's orders (auth: lb_session) |
| GET | `/dashboard/orders/{order_id}` | `dashboard_order` | Order status + intake form (auth: lb_session) |
| POST | `/dashboard/orders/{order_id}/ssn` | `dashboard_submit_ssn` | Submit SSN to vault (auth: lb_session + CSRF) |
| POST | `/dashboard/orders/{order_id}/complete-intake` | `dashboard_complete_intake` | Submit post-payment intake (auth: lb_session + CSRF) |
| GET | `/dashboard/orders/{order_id}/timeline` | `dashboard_order_timeline` | htmx timeline partial (auth: lb_session) |
| GET | `/orders/{order_id}/documents/{doc_id}` | `get_order_document` | Download document via signed GCS URL (auth: lb_session) |
| GET | `/admin` | `admin_dashboard` | Admin dashboard (HTTP Basic auth) |
| POST | `/admin/{order_id}/approve` | `admin_approve` | Approve → kick off SCC filing + document generation |
| POST | `/admin/{order_id}/mark-filed` | `admin_mark_filed` | Manual: mark SCC confirmed with confirmation number |
| POST | `/admin/{order_id}/apply-ein` | `admin_apply_ein` | Trigger EIN filing (IRS-hours-gated) |
| GET | `/admin/ein-filing-link/{order_id}` | `admin_ein_filing_link` | One-click EIN filing from admin email (HMAC token auth) |
| GET | `/admin/{order_id}/view-dashboard` | `admin_view_dashboard` | Admin: impersonate customer via fresh magic link |
| POST | `/admin/{order_id}/mark-ein` | `admin_mark_ein` | Manual: enter EIN number after unreadable confirmation |
| POST | `/admin/{order_id}/retry-agents` | `admin_retry_agents` | Retry failed document/asset generation agents |
| POST | `/admin/{order_id}/regenerate-website` | `admin_regenerate_website` | Force-redeploy website |
| POST | `/contact` | `contact` | Contact form (htmx partial swap or full page) |
| GET | `/health` | `health` | Health check → `{"status": "ok"}` |

---

## 4. Firestore Collections and Fields

### `orders` (or `ORDERS_COLLECTION` env var)

Core order document. Fields set at various pipeline stages:

**Created at /launch (pre-payment):**
| Field | Type | Notes |
|-------|------|-------|
| `state` | string | Order state machine value |
| `created_at` | timestamp | SERVER_TIMESTAMP |
| `checkout_at` | timestamp | SERVER_TIMESTAMP |
| `business_name` | string | Derived from desired_name or existing_llc_name |
| `business_idea` | string | Step 1 input |
| `desired_name` | string | Step 1 input |
| `first_name`, `last_name`, `middle_name` | string | Step 2 inputs |
| `full_name` | string | Derived: "First Middle Last" |
| `email`, `phone` | string | Step 2 inputs |
| `registered_agent_choice` | string | "launchbridge" or "self" |
| `skip_llc_formation` | bool | Existing LLC path |
| `existing_llc_name` | string | Existing LLC path only |
| `awaiting_intake` | bool | True for new wizard flow |
| `awaiting_ssn` | bool | True until SSN entered |
| `stripe_checkout_session_id` | string | Stripe session ID |
| `lead_id` | string | Optional: abandoned cart lead ID |

**After payment (/success or /webhook):**
| Field | Type | Notes |
|-------|------|-------|
| `paid_at` | timestamp | SERVER_TIMESTAMP |
| `name_check` | map | `{available, status, message, conflicts, raw}` |
| `name_cleared_at` | timestamp | When name_check passed |

**Post-payment intake (dashboard):**
| Field | Type | Notes |
|-------|------|-------|
| `dob` | string | Date of birth |
| `address`, `city`, `zipcode`, `county` | string | Principal address |
| `principal_address` | string | Derived: "addr, city, VA zip" |
| `business_purpose` | string | IRS EIN description |
| `target_customer` | string | For AI agents |
| `sig_first`, `sig_middle`, `sig_last` | string | Primary signer |
| `primary_sig` | string | Derived full signature name |
| `all_signatures` | array | [primary_sig, ...extra members] |
| `skip_ein` | bool | Customer already has EIN |
| `existing_ein` | string | skip_ein path only |
| `website_template` | string | "professional"/"local"/"modern" |
| `website_tagline` | string | Optional custom tagline |
| `website_description` | string | Optional custom about text |
| `service_1_name`...`service_3_desc` | string | Optional custom services |
| `business_hours` | string | Optional |
| `instagram_url`, `facebook_url`, `tiktok_url` | string | Optional |
| `color_preference` | string | "default"/"brand_kit"/"custom" |
| `custom_primary_color` | string | Hex, custom path only |
| `industry_code` | string | SCC industry code |
| `photo_1_data`...`photo_3_data` | string | base64 JPEG |
| `awaiting_intake` | bool | Cleared to false |
| `intake_complete_at` | timestamp | SERVER_TIMESTAMP |

**SSN vault:**
| Field | Type | Notes |
|-------|------|-------|
| `ssn_encrypted` | string | base64 KMS ciphertext |
| `ssn_collected_at` | timestamp | For 72h expiry |
| `ssn_expired` | bool | Set by expiry scheduler |

**Document generation (run_document_generation):**
| Field | Type | Notes |
|-------|------|-------|
| `brand_result` | map | Full brand data from Gemini |
| `marketing_result` | map | Marketing plan |
| `name_result` | map | Name screening result |
| `documents` | map | Nested doc refs (see below) |
| `documents.articles` | map | `{object_name, uploaded_at}` |
| `documents.operating_agreement` | map | Same object as articles |
| `documents.brand_kit` | map | `{object_name, uploaded_at}` |
| `documents.logo` | map | `{object_name, uploaded_at}` SVG |
| `documents_generated` | bool | All agents succeeded |
| `documents_generated_at` | timestamp | SERVER_TIMESTAMP |
| `documents_error` | string | Concatenated agent errors |

**Filing:**
| Field | Type | Notes |
|-------|------|-------|
| `filing_submitted_at` | timestamp | SERVER_TIMESTAMP |
| `filing_error` | string | Set on failure |
| `scc_confirmation_number` | string | 15-digit SCC reference |
| `filing_confirmed_at` | timestamp | SERVER_TIMESTAMP |

**EIN:**
| Field | Type | Notes |
|-------|------|-------|
| `ein_status` | string | "queued" when IRS closed at confirmation time |
| `next_available_window` | string | ISO datetime for queued EIN |
| `ein_submitted_to_irs` | bool | Durable guard against double-filing |
| `ein_submitted_at` | timestamp | SERVER_TIMESTAMP |
| `ein` | string | "XX-XXXXXXX" |
| `ein_issued_at` | timestamp | SERVER_TIMESTAMP |
| `ein_error` | string | Set on failure |
| `documents.ein_letter` | map | `{object_name, uploaded_at}` CP575 |

**Asset generation:**
| Field | Type | Notes |
|-------|------|-------|
| `stripe_connect_account_id` | string | Stripe Standard account ID |
| `stripe_payment_link_url` | string | Pay-what-you-want link URL |
| `website_url` | string | `https://{GITHUB_USERNAME}.github.io/{repo}` |
| `website_repo` | string | GitHub repo name |
| `website_content` | map | Gemini-generated content |
| `assets_generated_at` | timestamp | SERVER_TIMESTAMP |
| `asset_generation_error` | string | Set on failure |
| `fulfilled_at` | timestamp | SERVER_TIMESTAMP (complete state) |

**`orders/{order_id}/events` subcollection:**
Each doc: `{state, at}` — append-only audit trail written by `record_state()`, plus `{event: "ssn_deleted", at}` from `delete_ssn()`.

---

### `leads`

Abandoned cart recovery. Written by `/api/capture-lead`.

| Field | Type | Notes |
|-------|------|-------|
| `business_idea` | string | Step 1 |
| `desired_name` | string | Step 1 |
| `landing_page` | string | "home"/"pricing"/etc |
| `step1_at` | timestamp | SERVER_TIMESTAMP |
| `email`, `first_name`, `last_name`, `phone` | string | Step 2 |
| `step2_at` | timestamp | SERVER_TIMESTAMP |
| `converted` | bool | True once a non-draft order exists for this email |
| `recovery_1h_sent` | bool | 1-hour abandoned cart email sent |
| `recovery_24h_sent` | bool | 24-hour abandoned cart email sent |

---

### `webhook_events`

Stripe webhook log. Written by `/webhook`.

| Field | Type | Notes |
|-------|------|-------|
| `verified` | bool | Signature check passed |
| `event_id` | string | Stripe event ID |
| `event_type` | string | e.g. "checkout.session.completed" |
| `order_id` | string | From client_reference_id |
| `advanced_order` | bool | Whether process_paid_order acted |
| `received_at` | timestamp | SERVER_TIMESTAMP |
| `error` | string | Signature failure message |

---

### `magic_link_requests`

Rate limiting for dashboard magic link requests.

| Field | Type | Notes |
|-------|------|-------|
| `email` | string | Requester email |
| `at` | timestamp | SERVER_TIMESTAMP |

---

### `document_access_log`

Security audit log for document downloads.

| Field | Type | Notes |
|-------|------|-------|
| `customer_id` | string | Email of authenticated customer |
| `doc_id` | string | e.g. "brand_kit", "ein_letter" |
| `order_id` | string | |
| `session_id` | string | lb_session cookie value |
| `at` | timestamp | SERVER_TIMESTAMP |

---

### `contact_messages`

Visitor contact form submissions (from website contact forms and dashboard).

| Field | Type | Notes |
|-------|------|-------|
| `order_id`, `business_name` | string | Optional |
| `name`, `email`, `message` | string | |
| `created_at` | timestamp | SERVER_TIMESTAMP |

---

### `system/gmail_poller`

Single document tracking gmail poller health.

| Field | Type | Notes |
|-------|------|-------|
| `last_checked_at` | timestamp | Last poll run |
| `status` | string | "ok" or "error" |
| `last_error` | string | Error message if status=="error" |

---

### `processed_scc_emails`

Gmail poller: SCC approval emails seen (keyed by Gmail message ID).

| Field | Type | Notes |
|-------|------|-------|
| `gmail_message_id` | string | Gmail message ID |
| `subject` | string | Email subject |
| `processed_at` | timestamp | SERVER_TIMESTAMP |
| `matched` | bool | Found a matching order |
| `order_id` | string | If matched |
| `business_name` | string | If matched |
| `forwarded` | bool | Customer email forwarded |
| `error` | string | If any step failed |

---

### `lb_sessions` (managed by `app/dashboard_auth.py`)

Customer dashboard sessions.

| Field | Type | Notes |
|-------|------|-------|
| `customer_id` | string | Email address |
| `created_at` | timestamp | |
| `last_seen_at` | timestamp | Touched on every request |

---

## 5. Secret Manager Secrets

All loaded via `app/secrets.get_secret()`. Staging variants use `_STAGING` suffix.

| Secret Name | Description |
|-------------|-------------|
| `STRIPE_SECRET_KEY` | Stripe platform secret key (live) |
| `STRIPE_SECRET_KEY_STAGING` | Stripe platform secret key (test) |
| `STRIPE_PUBLISHABLE_KEY` | Stripe publishable key (live) |
| `STRIPE_PUBLISHABLE_KEY_STAGING` | Stripe publishable key (test) |
| `STRIPE_WEBHOOK_SECRET` | Webhook signing secret (live) |
| `STRIPE_WEBHOOK_SECRET_STAGING` | Webhook signing secret (test) |
| `GITHUB_TOKEN` | GitHub PAT for `launchbridge-sites` org (repo create, pages enable) |
| `ADMIN_PASSWORD` | HTTP Basic auth password for `/admin` |
| `SCC_FILING_CARD` | JSON: `{first_name, last_name, zip, address_line1, city, state, email, phone, card_number, exp_month, exp_year, cvc}` |
| `GMAIL_USER` | Gmail address for outbound email + SMS |
| `GMAIL_APP_PASSWORD` | Gmail App Password |
| `ADMIN_PHONE_EMAIL` | Carrier email-to-SMS gateway address |
| `STATUS_SESSION_SECRET` | HMAC key for magic links, sessions, CSRF, EIN-filing tokens |
| `SCC_USERNAME` | SCC CIS portal login (used by scc_llc_filer.py) |
| `SCC_PASSWORD` | SCC CIS portal password (used by scc_llc_filer.py) |

**Environment variables (not secrets):**

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_ENV` | `production` | Set to `staging` to use `_STAGING` secrets |
| `FIREBASE_PROJECT_ID` | `ai-biz-launcher` | GCP project |
| `ORDERS_COLLECTION` | `orders` | Firestore collection name |
| `STORAGE_BUCKET` | `ai-biz-launcher-llc-certificates` | GCS bucket for documents |
| `GITHUB_USERNAME` | `""` | GitHub org/user for deployed websites |
| `GOOGLE_PLACES_API_KEY` | `""` | Address autocomplete (optional) |
| `GOOGLE_ANALYTICS_ID` | `""` | GA4 measurement ID (optional) |
| `SAMPLE_WEBSITE_URL` | `""` | Demo site URL for homepage iframe |
| `ENABLE_EIN_SCHEDULER` | `true` | Set `false` on Cloud Run |

---

## 6. Order State Machine

```
draft
  │ process_paid_order() — /success or /webhook
  ↓
paid
  │ run_name_check() — background task after payment
  ↓
name_cleared
  │ admin approves via /admin/{id}/approve
  ↓
review_approved
  │ run_scc_filing() — background task
  ↓
filing_submitted
  │ Gmail poller OR check_scc_status OR admin mark-filed
  ↓
filing_confirmed  ←──── advance_past_filing_confirmed()
  │                     (also reachable directly for skip_llc_formation orders)
  ↓
awaiting_ein_filing   (skip_ein orders jump to ein_issued here)
  │ admin Apply for EIN click OR ein_queue_scheduler
  ↓
ein_requested
  │ IRS confirmation page read back
  ↓
ein_issued
  │ run_asset_generation() — background task
  ↓
assets_generated
  │ website deploy succeeds
  ↓
complete
```

**Special flags that modify the path:**
- `skip_llc_formation=True` — bypasses filing steps; jumps to `filing_confirmed` immediately after intake
- `skip_ein=True` — bypasses EIN application; uses `existing_ein`; jumps to `ein_issued` at `filing_confirmed`
- `awaiting_intake=True` — pipeline pauses after payment until post-payment intake form is submitted
- `awaiting_ssn=True` — pipeline pauses until SSN entered in dashboard
- `ein_status="queued"` — EIN application deferred to `next_available_window` (IRS closed at confirmation time)
- `ein_submitted_to_irs=True` — permanent guard: EIN was filed, never re-file even if confirmation page crashed

**State message helpers:**
- `compute_state_message(order, state)` — customer-facing status string
- `compute_timeline(order, state)` — 10-step timeline array for dashboard
- `reached(state, milestone)` — ordinal comparison helper

---

## 7. Deployed vs Local Only

### Deployed to Cloud Run (`main` branch = production, `staging` branch = staging)

| Component | Notes |
|-----------|-------|
| `app/main.py` (FastAPI app) | All HTTP routes, webhook, schedulers |
| `app/config.py` | Config + secrets loading |
| `app/agents/` (all) | Brand, website, LLC, marketing, name agents |
| `app/stripe_service.py` | All Stripe API calls |
| `app/ssn_vault.py` | KMS SSN encrypt/decrypt |
| `app/sms.py` | Admin SMS via Gmail SMTP |
| `app/email_service.py` | All customer + admin emails |
| `app/deployer.py` | GitHub Pages website deployment |
| `app/document_store.py` | GCS document storage + signed URLs |
| `app/dashboard_auth.py` | Magic link + session management |
| `app/dashboard_security.py` | CSRF tokens + security headers |
| `app/validators.py` | Form validation |
| `app/photo_utils.py` | Photo upload processing |
| `app/log_scrub.py` | SSN scrubbing from log messages |
| `app/utils/irs_hours.py` | IRS business hours calculator |
| `app/templates/` (all) | Jinja2 templates |
| `app/static/` | Static assets |
| `ssn_expiry_scheduler` | asyncio coroutine (safe on Cloud Run) |
| `abandoned_cart_scheduler` | asyncio coroutine (safe on Cloud Run) |

**Disabled on Cloud Run:**
- `ein_queue_scheduler` — requires local Chrome CDP; set `ENABLE_EIN_SCHEDULER=false`

---

### Local Only (require Chrome CDP at `http://172.27.176.1:9222`)

| Component | How to Run | Notes |
|-----------|-----------|-------|
| `app/scc_llc_filer.py` | Called by `run_scc_filing()` | Playwright → Chrome CDP → SCC CIS portal |
| `app/ein_filer.py` | Called by `run_ein_filing()` | Playwright → Chrome CDP → IRS EIN Assistant |
| `app/check_scc_status.py` | Standalone / called by admin app | Checks SCC for filed order confirmations |
| `app/gmail_poller.py` | `python3 -m app.gmail_poller` | Gmail OAuth2 → SCC approval email processing |
| `app/local_filing_poller.py` | Standalone | Alternative filing poller |
| `launch_bridge_admin.py` | `python launch_bridge_admin.py` | Tkinter desktop app wrapping the above |
| `save_scc_session.py` | Standalone | Save Chrome SCC cookies to disk |
| `keep_scc_alive.py` | Standalone | Ping SCC to keep session active |

---

### Local Development Credentials (never in git)

| File | Purpose |
|------|---------|
| `gmail_credentials.json` | Gmail OAuth2 client credentials |
| `gmail_token.json` | Gmail OAuth2 token cache |
| `scc_session.json` | SCC login cookies (written by save_scc_session.py) |
| `.env` | Local environment variable overrides |

---

### Cloud Infrastructure

| Resource | Details |
|----------|---------|
| **Cloud Run** | `app.launchbridge.ai` — FastAPI via Dockerfile |
| **Firestore** | Project `ai-biz-launcher`, default database |
| **Cloud KMS** | Project `ai-biz-launcher`, location `us-east4`, keyring `launchbridge-vault`, key `ssn-key` |
| **GCS** | Bucket `ai-biz-launcher-llc-certificates` — private, CMEK-encrypted |
| **Secret Manager** | Project `ai-biz-launcher` — all secrets listed in §5 |
| **GitHub Pages** | `{GITHUB_USERNAME}.github.io/{repo}` — one repo per customer website |
| **Stripe** | Platform account with Connect Standard; `GITHUB_USERNAME` env var controls website repo org |
