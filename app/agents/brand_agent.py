import base64
import json
import xml.etree.ElementTree as ET
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

def _is_well_formed_svg(svg: str) -> bool:
    """Gemini's plain-text output can look right at a glance (starts with
    <svg, ends with </svg>) while still being truncated or malformed mid-
    document - actually parsing it as XML catches that before it ever
    reaches a browser."""
    try:
        ET.fromstring(svg)
        return True
    except ET.ParseError:
        return False

def _generate_svg_from_prompt(prompt: str) -> str:
    """Shared plain-text-call-then-validate path for every logo/favicon
    variant below - same non-blocking philosophy as _generate_logo_svg:
    returns "" on any failure rather than ever raising, so a bad logo
    response can never block the rest of website/asset generation."""
    try:
        client = get_client()
        response = client.models.generate_content(model=MODEL, contents=prompt)
        svg = _strip_code_fence(response.text or "")
        if svg.startswith("<svg") and svg.endswith("</svg>") and _is_well_formed_svg(svg):
            return svg
        print("⚠️ Gemini logo response wasn't valid SVG markup.")
    except Exception as e:
        print(f"⚠️ Logo SVG generation failed: {e}")
    return ""

def generate_logo(business_name: str, primary_color: str, secondary_color: str, industry: str = "") -> str:
    """Wide-format (400x120) SVG logo with an icon AND the business name
    text, meant to actually be embedded on the customer's live website
    (nav/hero/footer) - distinct from _generate_logo_svg's icon-only
    monogram (200x200, no text), which stays as-is for the downloadable
    brand kit PDF. Takes the website's own resolved primary/secondary
    colors (whatever color_preference the customer picked - default,
    brand_kit, or custom) rather than brand_agent's independently-chosen
    palette, so the logo always visually matches the actual site it
    appears on."""
    display_name = _strip_entity_suffix(business_name)
    prompt = f"""
    Create a professional SVG logo for {business_name}.
    Industry: {industry or "general small business"}
    Primary color: {primary_color}
    Secondary color: {secondary_color}

    Requirements:
    - Clean, modern, professional design
    - Must work at small sizes (favicon) and large sizes (header)
    - Include both an icon/symbol AND the business name text
    - Icon should be simple and memorable - geometric shapes, initials, or simple symbol
    - Use the provided colors
    - SVG viewBox should be "0 0 400 120" (wide format for headers)
    - Use "{display_name}" as the logo text (not the full legal name with its entity
      suffix - real logos almost never include "LLC"/"Inc"/etc.), in a clean sans-serif
      font. "{display_name}" is {len(display_name)} characters - pick a font-size small
      enough that it fits within the viewBox width without being clipped (estimate
      roughly 0.6x font-size per character, leaving room for the icon). Never let text
      extend past x="390"
    - Return ONLY the SVG code, nothing else, no markdown fences, no explanation
    """
    return _generate_svg_from_prompt(prompt)

def generate_logo_square(business_name: str, primary_color: str, secondary_color: str, industry: str = "") -> str:
    """Square format (icon above name) - fits a compact card or square
    social-profile-style placement better than the wide horizontal logo."""
    display_name = _strip_entity_suffix(business_name)
    prompt = f"""
    Create a professional SVG logo for {business_name}, square/stacked layout.
    Industry: {industry or "general small business"}
    Primary color: {primary_color}
    Secondary color: {secondary_color}

    Requirements:
    - SVG viewBox should be "0 0 240 280" (tall square format)
    - Icon/symbol centered near the top, "{display_name}" centered below it as the logo
      text (not the full legal name with its entity suffix - real logos almost never
      include "LLC"/"Inc"/etc.), sized to fit within the viewBox width without clipping
    - Clean, modern, professional design using the provided colors
    - Icon should be simple and memorable - geometric shapes, initials, or simple symbol
    - Return ONLY the SVG code, nothing else, no markdown fences, no explanation
    """
    return _generate_svg_from_prompt(prompt)

def _strip_entity_suffix(business_name: str) -> str:
    """Every business name in this app ends in a mandatory entity suffix
    (LLC, and occasionally Inc/Corp for pre-existing entities) - real logos
    (and initials derived from them) almost never include it, so every
    logo/favicon/initials generator works from this stripped display name
    instead of the full legal name."""
    import re
    name = re.sub(r"\b(LLC|L\.L\.C\.|Inc|Incorporated|Corp|Corporation|Co)\.?\s*$", "", business_name, flags=re.IGNORECASE).strip()
    return name or business_name

def _initials(business_name: str) -> str:
    name = _strip_entity_suffix(business_name)
    letters = [w[0].upper() for w in name.split() if w and w[0].isalpha()]
    return "".join(letters[:3]) or (business_name[:1].upper() or "?")

def generate_logo_initials(business_name: str, primary_color: str, secondary_color: str) -> str:
    """Compact initials-only mark - the tightest format, good for
    favicon-adjacent use or anywhere the full name doesn't fit."""
    initials = _initials(business_name)
    prompt = f"""
    Create a simple, professional SVG monogram logo using the initials "{initials}"
    for a business called {business_name}.
    Primary color: {primary_color}
    Secondary color: {secondary_color}

    Requirements:
    - SVG viewBox should be "0 0 120 120" (compact square format)
    - Just the initials "{initials}" in a clean geometric mark (a circle, rounded
      square, or simple shape) - no other text
    - Must be legible at very small sizes
    - Return ONLY the SVG code, nothing else, no markdown fences, no explanation
    """
    return _generate_svg_from_prompt(prompt)

def generate_logo_variations(business_name: str, primary_color: str, secondary_color: str, industry: str = "") -> dict:
    """Generates all 3 logo formats. The website always uses "horizontal"
    as its embedded logo; "square" and "initials" are stored for the
    dashboard's brand kit display and future use (e.g. social profile
    photos) rather than picked between - each format serves a different
    placement, not a single "best" choice."""
    return {
        "horizontal": generate_logo(business_name, primary_color, secondary_color, industry),
        "square": generate_logo_square(business_name, primary_color, secondary_color, industry),
        "initials": generate_logo_initials(business_name, primary_color, secondary_color),
    }

def generate_favicon_svg(business_name: str, primary_color: str) -> str:
    """Deterministic, no Gemini call needed - a favicon is just the
    business's first 1-2 initials on a solid rounded square, so there's
    no reason to pay for/wait on an AI call for it."""
    initials = _initials(business_name)[:2]
    return (
        f'<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="32" height="32" rx="6" fill="{primary_color}"/>'
        f'<text x="16" y="22" font-family="Arial" font-weight="bold" font-size="18" '
        f'fill="white" text-anchor="middle">{initials}</text></svg>'
    )

def svg_to_data_uri(svg: str) -> str:
    """"" in, "" out - callers can pass a possibly-empty generated SVG
    straight through without a separate truthiness check first."""
    if not svg:
        return ""
    return f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}"

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
