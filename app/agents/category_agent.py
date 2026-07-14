import json

from google.genai import types
from app.agents import generate_content

MODEL = "gemini-2.5-flash"

# Fixed taxonomy - the confirmation dropdown in Step 5 (dashboard_business.html)
# and the classifier's response_schema below both draw from this same list, so
# it can't drift between what the customer sees and what the AI is allowed
# to pick.
CATEGORY_TAXONOMY = [
    "Consulting & Professional Services",
    "Technology & Software",
    "Real Estate & Property",
    "Construction & Trades",
    "Home & Cleaning Services",
    "Health & Wellness",
    "Beauty & Personal Care",
    "Food & Hospitality",
    "Retail & E-commerce",
    "Creative, Media & Events",
    "Transportation & Logistics",
    "Other / Not sure",
]

# Below this, the confirmation UI defaults the dropdown open/prominent
# instead of collapsed, since a low-confidence guess is more likely to
# need correcting.
LOW_CONFIDENCE_THRESHOLD = 0.6

CATEGORY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "category": {"type": "STRING", "enum": CATEGORY_TAXONOMY},
        "confidence": {"type": "NUMBER", "description": "0.0-1.0 confidence that this is the right category"},
        "government_contracting_signal": {
            "type": "BOOLEAN",
            "description": "True if the business idea text mentions or strongly implies government, defense, or federal contracting work",
        },
    },
    "required": ["category", "confidence", "government_contracting_signal"],
}

_FALLBACK = {"category": "Other / Not sure", "confidence": 0.0, "government_contracting_signal": False}


def classify_business_category(business_idea: str) -> dict:
    """Classifies a free-text business idea into one of CATEGORY_TAXONOMY,
    for the AI-suggested category shown (and editable) in Step 5. Never
    raises - a bad or unavailable classification falls back to "Other /
    Not sure" rather than blocking order creation or idea capture, both of
    which have nothing to do with this feature."""
    business_idea = (business_idea or "").strip()
    if not business_idea:
        return dict(_FALLBACK)

    prompt = f"""You are classifying a new small business into a fixed category taxonomy.

Business idea: {business_idea}

Pick exactly one category from this list that best fits the business:
{chr(10).join(f"- {c}" for c in CATEGORY_TAXONOMY)}

Use "Other / Not sure" only when nothing else is a confident match.

Also decide whether this business idea signals government or defense
contracting work (e.g. federal contracts, GSA schedules, SAM.gov,
defense/military clients) - true or false.

Give a confidence score from 0.0 to 1.0 for your category choice."""

    try:
        response = generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CATEGORY_SCHEMA,
            ),
        )
        result = json.loads(response.text)
        category = result.get("category")
        if category not in CATEGORY_TAXONOMY:
            return dict(_FALLBACK)
        confidence = float(result.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        return {
            "category": category,
            "confidence": confidence,
            "government_contracting_signal": bool(result.get("government_contracting_signal", False)),
        }
    except Exception as e:
        print(f"⚠️ Could not classify business category: {e}")
        return dict(_FALLBACK)
