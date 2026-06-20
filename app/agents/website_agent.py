import os
import re
from google.genai import types
from app.agents import get_client

MODEL = "gemini-2.5-flash"

TEMPLATE_FILES = {
    "professional": "professional.html",
    "local": "local.html",
    "modern": "modern.html",
}

CONTENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "tagline": {
            "type": "STRING",
            "description": "A punchy 5-10 word headline for the homepage hero section"
        },
        "about_text": {
            "type": "STRING",
            "description": "2-4 sentence paragraph describing the business, written for customers (not investors)"
        },
        "services": {
            "type": "ARRAY",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING", "description": "Short service name, 1-4 words"},
                    "description": {"type": "STRING", "description": "1-2 sentence description of this service"}
                },
                "required": ["name", "description"]
            }
        },
        "primary_color": {
            "type": "STRING",
            "pattern": "^#[0-9A-Fa-f]{6}$",
            "description": "Hex color code, e.g. #1A4D8F"
        },
        "secondary_color": {
            "type": "STRING",
            "pattern": "^#[0-9A-Fa-f]{6}$",
            "description": "Hex color code, e.g. #F2A93B"
        },
        "template": {
            "type": "STRING",
            "enum": ["professional", "local", "modern"],
            "description": "professional = consultants/services/finance, local = restaurants/retail/home services, modern = tech/creative/online businesses"
        }
    },
    "required": ["tagline", "about_text", "services", "primary_color", "secondary_color", "template"]
}

def generate_website_content(business_name: str, business_idea: str, target_customer: str) -> dict:
    client = get_client()

    prompt = f"""
    You are a copywriter and brand designer creating website content for a new Virginia small business.

    Business Name: {business_name}
    Business Idea: {business_idea}
    Target Customer: {target_customer}

    Write website copy tailored specifically to this business - not generic placeholder text.
    Pick exactly 3 services/offerings this business would realistically provide, each with its own name and description.
    Choose a primary and secondary brand color (as hex codes) that fit the business's industry and vibe.
    Choose which website template best fits this business.
    """

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CONTENT_SCHEMA
        )
    )

    import json
    return json.loads(response.text)

def render_website_html(content: dict, business_name: str, email: str, phone: str, address: str, template_override: str = None) -> tuple[str, str]:
    """Returns (html, template_name) with all placeholders filled in."""
    template_name = template_override if template_override in TEMPLATE_FILES else content.get("template", "professional")
    if template_name not in TEMPLATE_FILES:
        template_name = "professional"

    template_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "websites", TEMPLATE_FILES[template_name]
    )
    with open(template_path, "r") as f:
        html = f.read()

    services = content["services"]
    replacements = {
        "{{BUSINESS_NAME}}": business_name,
        "{{TAGLINE}}": content["tagline"],
        "{{ABOUT_TEXT}}": content["about_text"],
        "{{PRIMARY_COLOR}}": content["primary_color"],
        "{{SECONDARY_COLOR}}": content["secondary_color"],
        "{{PHONE}}": phone,
        "{{EMAIL}}": email,
        "{{ADDRESS}}": address,
    }
    for i, service in enumerate(services[:3], start=1):
        replacements[f"{{{{SERVICE_{i}_NAME}}}}"] = service["name"]
        replacements[f"{{{{SERVICE_{i}_DESC}}}}"] = service["description"]

    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)

    return html, template_name

def generate_website(business_name: str, business_idea: str, target_customer: str,
                      email: str, phone: str, address: str, template_override: str = None) -> dict:
    """Generates tailored website content and returns the rendered HTML plus metadata."""
    content = generate_website_content(business_name, business_idea, target_customer)
    html, template_name = render_website_html(content, business_name, email, phone, address, template_override)
    return {
        "html": html,
        "template": template_name,
        "content": content,
    }
