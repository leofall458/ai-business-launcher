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
            "description": "Five-color palette - all hex codes must be cohesive and fit the business's vibe.",
            "properties": {
                "primary": {"type": "OBJECT", "properties": {
                    "hex": {"type": "STRING", "pattern": "^#[0-9A-Fa-f]{6}$"}, "name": {"type": "STRING"}},
                    "required": ["hex", "name"]},
                "secondary": {"type": "OBJECT", "properties": {
                    "hex": {"type": "STRING", "pattern": "^#[0-9A-Fa-f]{6}$"}, "name": {"type": "STRING"}},
                    "required": ["hex", "name"]},
                "background": {"type": "OBJECT", "properties": {
                    "hex": {"type": "STRING", "pattern": "^#[0-9A-Fa-f]{6}$"}, "name": {"type": "STRING"}},
                    "required": ["hex", "name"]},
                "text": {"type": "OBJECT", "properties": {
                    "hex": {"type": "STRING", "pattern": "^#[0-9A-Fa-f]{6}$"}, "name": {"type": "STRING"}},
                    "required": ["hex", "name"]},
                "light": {"type": "OBJECT", "properties": {
                    "hex": {"type": "STRING", "pattern": "^#[0-9A-Fa-f]{6}$"}, "name": {"type": "STRING"}},
                    "required": ["hex", "name"]},
            },
            "required": ["primary", "secondary", "background", "text", "light"],
        },
        "fonts": {
            "type": "OBJECT",
            "properties": {
                "heading": {"type": "STRING", "description": "A real Google Font name, bold and distinctive"},
                "body": {"type": "STRING", "description": "A real Google Font name, readable and clean"},
                "pairing_rationale": {"type": "STRING", "description": "1-2 sentences on why these two fonts work together"},
            },
            "required": ["heading", "body", "pairing_rationale"],
        },
        "taglines": {
            "type": "ARRAY", "minItems": 3, "maxItems": 3,
            "items": {"type": "STRING", "description": "Under 8 words, punchy, professional"},
        },
        "voice": {
            "type": "OBJECT",
            "properties": {
                "tone": {"type": "STRING", "description": "One of: professional, friendly, authoritative, casual - or a short blend"},
                "values": {"type": "ARRAY", "minItems": 3, "maxItems": 3, "items": {"type": "STRING"}},
                "audience": {"type": "STRING", "description": "1-2 sentence description of the target customer"},
                "story": {"type": "STRING", "description": "2-3 sentence brand story capturing mission and personality"},
            },
            "required": ["tone", "values", "audience", "story"],
        },
        "social": {
            "type": "OBJECT",
            "properties": {
                "photo_style": {"type": "STRING", "description": "Recommended profile photo style description"},
                "post_templates": {"type": "ARRAY", "minItems": 3, "maxItems": 3, "items": {"type": "STRING"}},
                "hashtags": {"type": "ARRAY", "minItems": 5, "maxItems": 8, "items": {"type": "STRING"}},
            },
            "required": ["photo_style", "post_templates", "hashtags"],
        },
    },
    "required": ["colors", "fonts", "taglines", "voice", "social"],
}

def _generate_brand_data(business_name: str, business_idea: str, target_customer: str) -> dict:
    client = get_client()
    prompt = f"""
    You are a brand designer creating a complete, cohesive brand identity for a new Virginia small business.

    Business Name: {business_name}
    Business Idea: {business_idea}
    Target Customer: {target_customer}

    Design a 5-color palette (primary, secondary, background, text, light/neutral) that works
    well together and fits this business's vibe. Recommend a heading/body Google Font pairing
    with a real rationale. Write 3 short, memorable taglines. Define the brand voice (tone, 3
    core values, target audience, and a short brand story). Build a social media kit (profile
    photo style, 3 sample post templates, relevant hashtags).

    Make everything feel like one cohesive brand, not a list of disconnected ideas.
    """
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=BRAND_SCHEMA,
        ),
    )
    return json.loads(response.text)

def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()

def _generate_logo_svg(business_name: str, business_idea: str, primary_hex: str, secondary_hex: str) -> str:
    """A separate plain-text call (not the JSON schema above) since asking
    a JSON schema to carry raw SVG markup risks broken escaping. Returns
    "" on any failure or invalid output - callers fall back to a simple
    vector monogram (see app/agents/brand_pdf.py) rather than ever
    blocking brand kit generation on this one piece."""
    try:
        client = get_client()
        prompt = f"""
        Design a simple, professional SVG logo for this business:

        Business Name: {business_name}
        Business Idea: {business_idea}

        Requirements:
        - Clean and scalable - works as a small icon or large print
        - Must look good in both full color and black/white
        - Use primarily these brand colors: {primary_hex} and {secondary_hex}
        - Simple geometric shapes, a monogram, or an abstract mark - not a photorealistic illustration
        - viewBox="0 0 200 200", no external fonts or images referenced

        Respond with ONLY the raw <svg>...</svg> markup. No markdown code fences, no explanation, no XML declaration.
        """
        response = client.models.generate_content(model=MODEL, contents=prompt)
        svg = _strip_code_fence(response.text or "")
        if svg.startswith("<svg") and svg.endswith("</svg>"):
            return svg
        print("⚠️ Gemini logo response wasn't valid SVG markup - falling back to a generated monogram.")
        return ""
    except Exception as e:
        print(f"⚠️ Logo generation failed (non-fatal, falling back to a generated monogram): {e}")
        return ""

def generate_brand_kit(business_name: str, business_idea: str, target_customer: str,
                        full_name: str = "", email: str = "", phone: str = "",
                        website_url: str = "") -> dict:
    """Generates a full structured brand kit (palette, fonts, taglines,
    voice, social kit, SVG logo) and assembles it into a PDF brand kit
    document. Returns both the structured data (for Firestore + future
    reuse) and the rendered PDF bytes (for upload to document storage -
    the caller is responsible for stripping pdf_bytes before persisting
    the rest to Firestore, since a PDF is far too large for a document
    field there)."""
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
        "pdf_bytes": pdf_bytes,
    }
