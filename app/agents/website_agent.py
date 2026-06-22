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
    "professional": ("#1E3A5F", "#64748B"),
    "local": ("#9A3412", "#F59E0B"),
    "modern": ("#22D3EE", "#A855F7"),
}

_jinja_env = Environment(loader=FileSystemLoader(WEBSITES_DIR))

CONTENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "tagline": {"type": "STRING", "description": "A punchy 5-10 word headline for the homepage hero section"},
        "about_text": {"type": "STRING", "description": "2-4 sentence paragraph describing the business, written for customers (not investors)"},
        "services": {
            "type": "ARRAY", "minItems": 3, "maxItems": 3,
            "items": {"type": "OBJECT", "properties": {
                "name": {"type": "STRING", "description": "Short service name, 1-4 words"},
                "description": {"type": "STRING", "description": "1-2 sentence description of this service"}
            }, "required": ["name", "description"]}
        },
        "primary_color": {"type": "STRING", "pattern": "^#[0-9A-Fa-f]{6}$", "description": "Hex color code, e.g. #1A4D8F"},
        "secondary_color": {"type": "STRING", "pattern": "^#[0-9A-Fa-f]{6}$", "description": "Hex color code, e.g. #F2A93B"},
    },
    "required": ["tagline", "about_text", "services", "primary_color", "secondary_color"]
}

def generate_website_content(business_name: str, business_idea: str, target_customer: str) -> dict:
    """Calls Gemini for whatever copy/colors the customer didn't provide
    themselves. Always asks for all five fields - the caller picks and
    chooses which ones it actually needs per-field."""
    client = get_client()

    prompt = f"""
    You are a copywriter and designer creating website content for a new Virginia small business.

    Business Name: {business_name}
    Business Idea: {business_idea}
    Target Customer: {target_customer}

    Write a tagline, an about paragraph, three services this business would plausibly offer,
    and a primary/secondary hex color pair that fits the business's vibe.
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
        ai = ai_services[i] if ai_services and i < len(ai_services) else {"name": "", "description": ""}
        name = (customer.get("name") or "").strip() or ai.get("name", "")
        description = (customer.get("description") or "").strip() or ai.get("description", "")
        merged.append({"name": name, "description": description})
    return merged

def render_website_html(content: dict, business_name: str, email: str, phone: str, address: str,
                         template_name: str, payment_link_url: str = None,
                         hero_photo: str = None, gallery_photos: list = None,
                         hours: str = None, instagram_url: str = None,
                         facebook_url: str = None, tiktok_url: str = None) -> str:
    """Renders one of the Jinja2 website templates with the fully-resolved
    content dict (tagline/about_text/services/colors already merged by the
    caller)."""
    if template_name not in TEMPLATE_FILES:
        template_name = "professional"

    template = _jinja_env.get_template(TEMPLATE_FILES[template_name])
    return template.render(
        business_name=business_name,
        tagline=content["tagline"],
        about_text=content["about_text"],
        services=content["services"],
        primary_color=content["primary_color"],
        secondary_color=content["secondary_color"],
        email=email,
        phone=phone,
        address=address,
        payment_link_url=payment_link_url,
        hero_photo=hero_photo,
        gallery_photos=gallery_photos or [],
        hours=hours,
        instagram_url=instagram_url,
        facebook_url=facebook_url,
        tiktok_url=tiktok_url,
    )

def generate_website(
    business_name: str, business_idea: str, target_customer: str,
    email: str, phone: str, address: str,
    template_name: str = "professional",
    tagline: str = None, description: str = None,
    services: list = None,
    hours: str = None,
    photos: list = None,
    instagram_url: str = None, facebook_url: str = None, tiktok_url: str = None,
    color_preference: str = "default", custom_primary_color: str = None,
    payment_link_url: str = None,
) -> dict:
    """Top-level entry point used by main.py's asset generation step.
    Accepts every customer-provided customization field and fills any gap
    (tagline, description, services, colors) with Gemini-generated content,
    field by field rather than all-or-nothing."""
    if template_name not in TEMPLATE_FILES:
        template_name = "professional"

    services = services or [{}, {}, {}]
    tagline = (tagline or "").strip()
    description = (description or "").strip()
    services_need_ai = any(not (s.get("name") or "").strip() or not (s.get("description") or "").strip() for s in services[:3])
    need_ai = not tagline or not description or services_need_ai or color_preference == "brand_kit"

    ai_content = None
    if need_ai:
        ai_content = generate_website_content(business_name, business_idea, target_customer)

    final_tagline = tagline or (ai_content["tagline"] if ai_content else "")
    final_about = description or (ai_content["about_text"] if ai_content else "")
    final_services = _merge_services(services, ai_content["services"] if ai_content else None)

    if color_preference == "brand_kit" and ai_content:
        primary_color, secondary_color = ai_content["primary_color"], ai_content["secondary_color"]
    elif color_preference == "custom" and custom_primary_color:
        _, default_secondary = TEMPLATE_DEFAULT_COLORS[template_name]
        primary_color, secondary_color = custom_primary_color, default_secondary
    else:
        primary_color, secondary_color = TEMPLATE_DEFAULT_COLORS[template_name]

    content = {
        "tagline": final_tagline,
        "about_text": final_about,
        "services": final_services,
        "primary_color": primary_color,
        "secondary_color": secondary_color,
    }

    photos = [p for p in (photos or []) if p]
    html = render_website_html(
        content, business_name, email, phone, address,
        template_name=template_name,
        payment_link_url=payment_link_url,
        hero_photo=photos[0] if photos else None,
        gallery_photos=photos,
        hours=(hours or "").strip() or None,
        instagram_url=(instagram_url or "").strip() or None,
        facebook_url=(facebook_url or "").strip() or None,
        tiktok_url=(tiktok_url or "").strip() or None,
    )

    return {"html": html, "template": template_name, "content": content}
