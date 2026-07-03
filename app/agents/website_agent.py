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
        "tagline": {"type": "STRING", "description": "A specific, compelling 5-10 word hero headline - never generic filler like 'Quality you can trust', it must reference something concrete about this actual business"},
        "about_text": {"type": "STRING", "description": "2-4 sentence paragraph describing the business, written in a warm, human, first-person-plural voice ('we'), not corporate jargon"},
        "services": {
            "type": "ARRAY", "minItems": 3, "maxItems": 3,
            "items": {"type": "OBJECT", "properties": {
                "name": {"type": "STRING", "description": "Short service name, 1-4 words"},
                "description": {"type": "STRING", "description": "1-2 sentence description of this service, specific to this business, not generic"}
            }, "required": ["name", "description"]}
        },
        "cta_text": {"type": "STRING", "description": "A short call-to-action button label that fits this specific business, e.g. 'Book a Call', 'Order Now', 'Get a Quote', 'Reserve Your Spot'"},
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
    "required": ["tagline", "about_text", "services", "cta_text", "faq", "primary_color", "secondary_color"]
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
    other business in the same industry.

    Business Name: {business_name}
    Business Idea: {business_idea}
    Target Customer: {target_customer}

    Write:
    - A compelling, specific hero headline (tagline)
    - An authentic, human-sounding about paragraph
    - Three unique services this business would plausibly offer, each with its own real description
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
        ai = ai_services[i] if ai_services and i < len(ai_services) else {"name": "", "description": ""}
        name = (customer.get("name") or "").strip() or ai.get("name", "")
        description = (customer.get("description") or "").strip() or ai.get("description", "")
        merged.append({"name": name, "description": description})
    return merged

def render_website_html(content: dict, business_name: str,
                         template_name: str, payment_link_url: str = None,
                         hero_photo: str = None, gallery_photos: list = None,
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
        about_text=content["about_text"],
        services=content["services"],
        cta_text=content.get("cta_text") or "Get in Touch",
        faq=content.get("faq") or [],
        primary_color=content["primary_color"],
        secondary_color=content["secondary_color"],
        payment_link_url=payment_link_url,
        hero_photo=hero_photo,
        gallery_photos=gallery_photos or [],
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
) -> dict:
    """Top-level entry point used by main.py's asset generation step.
    Accepts every customer-provided customization field and fills any gap
    (tagline, description, services, colors) with Gemini-generated content,
    field by field rather than all-or-nothing. cta_text and faq have no
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
        "about_text": final_about,
        "services": final_services,
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
