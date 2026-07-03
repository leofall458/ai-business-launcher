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

# Used whenever color_preference is "default" - each template has its own
# baked-in look, not an AI-invented palette.
TEMPLATE_DEFAULT_COLORS = {
    "professional": ("#0e2148", "#c9a227"),
    "local": ("#d97706", "#78350f"),
    "modern": ("#3b82f6", "#22d3ee"),
}

_jinja_env = Environment(loader=FileSystemLoader(WEBSITES_DIR))

# Fallback keyword sets, keyed by a rough industry label - only used if the
# Gemini keyword call in get_image_keywords itself fails (network/quota/
# parsing error). Gemini reading the actual business_idea text is almost
# always more specific than a static table could be, so this is a safety
# net, not the primary path.
INDUSTRY_IMAGE_KEYWORDS = {
    "consulting": "office,business,professional",
    "it": "office,business,professional",
    "food": "restaurant,food,dining",
    "restaurant": "restaurant,food,dining",
    "home services": "home,tools,service",
    "health": "fitness,health,wellness",
    "fitness": "fitness,health,wellness",
    "retail": "retail,shop,store",
    "legal": "legal,document,signing",
    "notary": "legal,document,signing",
    "tech": "technology,computer,digital",
    "beauty": "beauty,salon,spa",
    "real estate": "realestate,house,property",
    "finance": "finance,money,business",
}
DEFAULT_IMAGE_KEYWORDS = "business,professional,success"

KEYWORD_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "keywords": {
            "type": "ARRAY", "minItems": 2, "maxItems": 3,
            "items": {"type": "STRING", "description": "A single lowercase English word suitable as a stock-photo search term (e.g. 'bakery', 'plumber', 'yoga') - no phrases, no punctuation"},
        },
    },
    "required": ["keywords"],
}

def get_image_keywords(business_idea: str, industry: str = None) -> str:
    """Asks Gemini for 2-3 stock-photo search keywords specific to this
    business, then builds a loremflickr.com URL from them - loremflickr is
    a free, keyless, keyword-driven photo service (source.unsplash.com,
    Unsplash's old free/keyless endpoint, was shut down some time ago and
    now just 503s; loremflickr is the closest live equivalent that doesn't
    need an API key or account).

    Falls back to INDUSTRY_IMAGE_KEYWORDS[industry] if that's a known
    label, or DEFAULT_IMAGE_KEYWORDS otherwise, whenever the Gemini call
    itself fails - a broken hero image is worse than a generic one, but a
    crashed asset-generation pipeline is worse still."""
    try:
        client = get_client()
        prompt = f"""
        A new small business describes itself as: "{business_idea}"

        Give 2-3 concrete, specific English keywords (single words, not
        phrases) that would find relevant stock photos for this business's
        website - think about what the business actually looks like or
        does, not generic words like "business" or "success".
        """
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=KEYWORD_SCHEMA,
            ),
        )
        keywords = json.loads(response.text)["keywords"]
        keyword_str = ",".join(k.strip().lower() for k in keywords if k.strip())
        if keyword_str:
            return keyword_str
    except Exception as e:
        print(f"⚠️ get_image_keywords Gemini call failed, falling back: {e}")

    industry_key = (industry or "").strip().lower()
    return INDUSTRY_IMAGE_KEYWORDS.get(industry_key, DEFAULT_IMAGE_KEYWORDS)

def _image_url(keywords: str, width: int, height: int, lock: int) -> str:
    return f"https://loremflickr.com/{width}/{height}/{keywords}?lock={lock}"

CONTENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "tagline": {"type": "STRING", "description": "A compelling, specific hero headline for this exact business, under 8 words, memorable and specific to what makes this business unique - never generic filler like 'Quality you can trust' or words like 'excellence'/'quality service'"},
        "hero_subheadline": {"type": "STRING", "description": "One punchy, concrete sentence (under 20 words) that expands on the tagline - what this business actually does and for whom, not a restatement of the tagline in different words"},
        "about_text": {"type": "STRING", "description": "3-4 sentence paragraph describing the business, written in a warm, human, first-person-plural voice ('we'), that sounds like a real owner talking to their community - not corporate jargon, not interchangeable with any other business in this industry"},
        "differentiators": {
            "type": "ARRAY", "minItems": 3, "maxItems": 4,
            "items": {"type": "STRING", "description": "A short (3-6 word) concrete differentiator specific to this business, e.g. 'Licensed & insured in Virginia' or 'Same-week turnaround' - not generic claims"},
        },
        "services": {
            "type": "ARRAY", "minItems": 3, "maxItems": 3,
            "items": {"type": "OBJECT", "properties": {
                "name": {"type": "STRING", "description": "Short service name, 1-4 words"},
                "description": {"type": "STRING", "description": "2-3 sentence description of this service, specific to this business, not generic"},
                "icon": {"type": "STRING", "description": "A single emoji that best represents this specific service"},
            }, "required": ["name", "description", "icon"]}
        },
        "why_choose_us": {
            "type": "ARRAY", "minItems": 3, "maxItems": 3,
            "items": {"type": "OBJECT", "properties": {
                "title": {"type": "STRING", "description": "Short (2-4 word) reason title"},
                "description": {"type": "STRING", "description": "1 sentence explaining this reason, specific to this business's industry - not generic corporate claims"},
            }, "required": ["title", "description"]}
        },
        "cta_text": {"type": "STRING", "description": "A short, strong call-to-action button label that matches this specific business type, e.g. 'Book a Call', 'Order Now', 'Get a Quote', 'Reserve Your Spot' - pick or invent whatever fits best"},
        "faq": {
            "type": "ARRAY", "minItems": 4, "maxItems": 5,
            "items": {"type": "OBJECT", "properties": {
                "question": {"type": "STRING", "description": "A real question a prospective customer of this specific business would ask"},
                "answer": {"type": "STRING", "description": "1-3 sentence answer"}
            }, "required": ["question", "answer"]}
        },
        "primary_color": {"type": "STRING", "pattern": "^#[0-9A-Fa-f]{6}$", "description": "Hex color code, e.g. #1A4D8F"},
        "secondary_color": {"type": "STRING", "pattern": "^#[0-9A-Fa-f]{6}$", "description": "Hex color code, e.g. #F2A93B"},
    },
    "required": ["tagline", "hero_subheadline", "about_text", "differentiators", "services", "why_choose_us", "cta_text", "faq", "primary_color", "secondary_color"]
}

def generate_website_content(business_name: str, business_idea: str, target_customer: str) -> dict:
    """Calls Gemini for whatever copy/colors the customer didn't provide
    themselves. Always asks for every field - the caller picks and
    chooses which ones it actually needs per-field."""
    client = get_client()

    prompt = f"""
    You are a copywriter and designer creating website content for a new Virginia small business.
    Write copy that sounds like a real small business owner talking to their community, not
    generic marketing filler - be specific to this actual business, not interchangeable with any
    other business in the same industry. Never use generic phrases like "excellence" or "quality
    service" anywhere in your output.

    Business Name: {business_name}
    Business Idea: {business_idea}
    Target Customer: {target_customer}

    Write a compelling, specific tagline for {business_name}, a business serving {target_customer}
    in Virginia. Make it memorable, under 8 words, and specific to what makes them unique - not a
    generic industry phrase that could apply to any competitor.

    Also write:
    - A one-sentence hero subheadline expanding on the tagline with something concrete
    - An authentic, human-sounding 3-4 sentence about paragraph
    - 3-4 short, specific differentiators (not generic claims)
    - Three unique services this business would plausibly offer, each with its own real
      description and a single fitting emoji icon
    - Three "why choose us" reasons specific to this business's industry, each with a short title
      and one-sentence explanation
    - A call-to-action button label that fits this specific business (e.g. "Book a Call", "Order Now",
      "Get a Quote", "Reserve Your Spot" - pick or invent whatever fits best)
    - 4-5 FAQ questions a real prospective customer of this business would actually ask, with answers
    - A primary/secondary hex color pair that fits the business's vibe
    """

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CONTENT_SCHEMA,
        ),
    )

    return json.loads(response.text)

def _merge_services(customer_services: list, ai_services: list) -> list:
    """Per-field fallback, not per-slot: a service with a name but no
    description still gets the customer's name paired with an AI
    description, rather than discarding it wholesale."""
    merged = []
    for i in range(3):
        customer = customer_services[i] if i < len(customer_services) else {}
        ai = ai_services[i] if ai_services and i < len(ai_services) else {"name": "", "description": "", "icon": "✨"}
        name = (customer.get("name") or "").strip() or ai.get("name", "")
        description = (customer.get("description") or "").strip() or ai.get("description", "")
        icon = ai.get("icon") or "✨"
        merged.append({"name": name, "description": description, "icon": icon})
    return merged

def render_website_html(content: dict, business_name: str,
                         template_name: str, payment_link_url: str = None,
                         hero_photo: str = None, gallery_photos: list = None,
                         hero_image_url: str = None, about_image_url: str = None,
                         hours: str = None, instagram_url: str = None,
                         facebook_url: str = None, tiktok_url: str = None,
                         linkedin_url: str = None,
                         order_id: str = None, site_url: str = None,
                         service_area: str = "Virginia",
                         contact_phone: str = None, contact_email: str = None,
                         contact_address: str = None) -> str:
    """Renders one of the Jinja2 website templates with the fully-resolved
    content dict (tagline/about_text/services/colors already merged by the
    caller).

    hero_photo/gallery_photos are the customer's own uploaded photos (Step
    6) and always take priority when present; hero_image_url/about_image_url
    are the stock-photo fallback from get_image_keywords, used only when the
    customer hasn't uploaded anything of their own.

    contact_phone/email/address are the customer's own opt-in choice of what
    to publish in the site's Contact section - see generate_website, which
    is the only caller and the single place that decides whether these are
    even non-None. Nothing else about the customer (their account email,
    personal phone, or the private address used for LLC filing) is ever
    passed into a public website template."""
    if template_name not in TEMPLATE_FILES:
        template_name = "professional"

    template = _jinja_env.get_template(TEMPLATE_FILES[template_name])
    return template.render(
        business_name=business_name,
        tagline=content["tagline"],
        hero_subheadline=content.get("hero_subheadline", ""),
        about_text=content["about_text"],
        differentiators=content.get("differentiators") or [],
        services=content["services"],
        why_choose_us=content.get("why_choose_us") or [],
        cta_text=content.get("cta_text") or "Get in Touch",
        faq=content.get("faq") or [],
        primary_color=content["primary_color"],
        secondary_color=content["secondary_color"],
        payment_link_url=payment_link_url,
        hero_photo=hero_photo,
        gallery_photos=gallery_photos or [],
        hero_image_url=hero_photo or hero_image_url,
        about_image_url=(gallery_photos or [None])[0] or about_image_url,
        hours=hours,
        instagram_url=instagram_url,
        facebook_url=facebook_url,
        tiktok_url=tiktok_url,
        linkedin_url=linkedin_url,
        order_id=order_id,
        site_url=site_url or "",
        service_area=service_area or "Virginia",
        contact_endpoint="https://app.launchbridge.ai/contact",
        contact_phone=contact_phone,
        contact_email=contact_email,
        contact_address=contact_address,
    )

def generate_website(
    business_name: str, business_idea: str, target_customer: str,
    template_name: str = "professional",
    tagline: str = None, description: str = None,
    services: list = None,
    hours: str = None,
    photos: list = None,
    instagram_url: str = None, facebook_url: str = None, tiktok_url: str = None,
    linkedin_url: str = None,
    color_preference: str = "default", custom_primary_color: str = None,
    payment_link_url: str = None, order_id: str = None, site_url: str = None,
    show_contact: bool = False, contact_phone: str = None,
    contact_email: str = None, contact_address: str = None,
    industry: str = None,
) -> dict:
    """Top-level entry point used by main.py's asset generation step.
    Accepts every customer-provided customization field and fills any gap
    (tagline, description, services, colors, stock photos) with
    Gemini-generated content, field by field rather than all-or-nothing.
    cta_text, faq, differentiators, and why_choose_us have no
    customer-provided equivalent, so Gemini is always called for those.

    show_contact gates contact_phone/email/address in one place: unless the
    customer explicitly opted in on the website customization step, all
    three are forced to None here regardless of what's stored on the order,
    so a public site can never end up displaying contact details the
    customer didn't choose to publish."""
    if not show_contact:
        contact_phone = contact_email = contact_address = None
    if template_name not in TEMPLATE_FILES:
        template_name = "professional"

    services = services or [{}, {}, {}]
    tagline = (tagline or "").strip()
    description = (description or "").strip()

    ai_content = generate_website_content(business_name, business_idea, target_customer)
    image_keywords = get_image_keywords(business_idea, industry)

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
        "tagline": final_tagline,
        "hero_subheadline": ai_content.get("hero_subheadline", ""),
        "about_text": final_about,
        "differentiators": ai_content.get("differentiators") or [],
        "services": final_services,
        "why_choose_us": ai_content.get("why_choose_us") or [],
        "cta_text": ai_content["cta_text"],
        "faq": ai_content["faq"],
        "primary_color": primary_color,
        "secondary_color": secondary_color,
    }

    photos = [p for p in (photos or []) if p]
    html = render_website_html(
        content, business_name,
        template_name=template_name,
        payment_link_url=payment_link_url,
        hero_photo=photos[0] if photos else None,
        gallery_photos=photos,
        hero_image_url=_image_url(image_keywords, 1600, 900, lock=1),
        about_image_url=_image_url(image_keywords, 900, 700, lock=2),
        hours=(hours or "").strip() or None,
        instagram_url=(instagram_url or "").strip() or None,
        facebook_url=(facebook_url or "").strip() or None,
        tiktok_url=(tiktok_url or "").strip() or None,
        linkedin_url=(linkedin_url or "").strip() or None,
        order_id=order_id,
        site_url=site_url,
        contact_phone=(contact_phone or "").strip() or None,
        contact_email=(contact_email or "").strip() or None,
        contact_address=(contact_address or "").strip() or None,
    )

    return {"html": html, "template": template_name, "content": content}
