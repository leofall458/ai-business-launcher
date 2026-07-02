import json
from google.genai import types
from app.agents import get_client

MODEL = "gemini-2.5-flash"

# Structured output, not freeform HTML from the model directly - same
# reasoning as brand_agent.py's BRAND_SCHEMA: letting Gemini emit raw HTML
# risks malformed markup landing straight in a customer email. Content
# comes back as clean, schema-validated data; _render_html below is the
# only thing that ever produces markup, with full control over styling.
MARKETING_PLAN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "top_channels": {
            "type": "ARRAY", "minItems": 3, "maxItems": 3,
            "description": "The 3 best marketing channels for this specific business, with a one-sentence rationale each.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "channel": {"type": "STRING"},
                    "rationale": {"type": "STRING"},
                },
                "required": ["channel", "rationale"],
            },
        },
        "week1_foundation": {
            "type": "OBJECT",
            "properties": {
                "google_business_profile": {"type": "STRING", "description": "Specific steps to set up Google Business Profile for this business"},
                "social_platforms": {"type": "ARRAY", "minItems": 2, "maxItems": 4, "items": {"type": "STRING"}, "description": "Which platforms fit this business type, e.g. Instagram, Facebook, LinkedIn, TikTok"},
                "target_audience": {"type": "STRING", "description": "A specific, concrete target audience definition"},
                "action_items": {"type": "ARRAY", "minItems": 3, "maxItems": 3, "items": {"type": "STRING"}},
            },
            "required": ["google_business_profile", "social_platforms", "target_audience", "action_items"],
        },
        "week2_content": {
            "type": "OBJECT",
            "properties": {
                "content_calendar": {"type": "ARRAY", "minItems": 5, "maxItems": 5, "items": {"type": "STRING"}, "description": "5 specific post ideas"},
                "sample_posts": {"type": "ARRAY", "minItems": 3, "maxItems": 3, "items": {"type": "STRING"}, "description": "3 fully written-out sample social posts, ready to copy-paste"},
                "hashtags": {"type": "ARRAY", "minItems": 10, "maxItems": 15, "items": {"type": "STRING"}},
                "action_items": {"type": "ARRAY", "minItems": 3, "maxItems": 3, "items": {"type": "STRING"}},
            },
            "required": ["content_calendar", "sample_posts", "hashtags", "action_items"],
        },
        "week3_outreach": {
            "type": "OBJECT",
            "properties": {
                "local_networking": {"type": "ARRAY", "minItems": 2, "maxItems": 4, "items": {"type": "STRING"}, "description": "Specific Virginia networking opportunities - real organization/event types, not generic advice"},
                "partnerships": {"type": "ARRAY", "minItems": 2, "maxItems": 4, "items": {"type": "STRING"}, "description": "Specific partnership ideas relevant to this business"},
                "email_template": {"type": "STRING", "description": "One full, ready-to-send outreach email template"},
                "action_items": {"type": "ARRAY", "minItems": 3, "maxItems": 3, "items": {"type": "STRING"}},
            },
            "required": ["local_networking", "partnerships", "email_template", "action_items"],
        },
        "week4_growth": {
            "type": "OBJECT",
            "properties": {
                "paid_ads_recommendation": {"type": "STRING", "description": "Google Ads vs Facebook/Instagram Ads vs other, with reasoning for this business"},
                "budget_recommendation": {"type": "STRING", "description": "A specific monthly ad-spend recommendation for a business at this stage"},
                "key_metrics": {"type": "ARRAY", "minItems": 3, "maxItems": 5, "items": {"type": "STRING"}},
                "action_items": {"type": "ARRAY", "minItems": 3, "maxItems": 3, "items": {"type": "STRING"}},
            },
            "required": ["paid_ads_recommendation", "budget_recommendation", "key_metrics", "action_items"],
        },
        "budget_breakdown": {
            "type": "ARRAY", "minItems": 3, "maxItems": 6,
            "description": "How to spend the first $500 in marketing budget, itemized",
            "items": {
                "type": "OBJECT",
                "properties": {"category": {"type": "STRING"}, "amount": {"type": "STRING"}},
                "required": ["category", "amount"],
            },
        },
        "goals_90_day": {
            "type": "ARRAY", "minItems": 3, "maxItems": 5,
            "items": {"type": "STRING"},
        },
    },
    "required": [
        "top_channels", "week1_foundation", "week2_content", "week3_outreach",
        "week4_growth", "budget_breakdown", "goals_90_day",
    ],
}


def _generate_plan_data(business_name: str, business_idea: str, target_customer: str,
                         industry: str, location: str) -> dict:
    client = get_client()
    prompt = f"""
    You are a marketing strategist creating a complete 30-day launch plan for a new small business.

    Business Name: {business_name}
    Business Idea: {business_idea}
    Industry: {industry}
    Target Customer: {target_customer}
    Location: {location}

    Be specific and actionable for THIS business - no generic advice that could apply to any
    company. Reference the actual industry, location, and target customer throughout.
    """
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=MARKETING_PLAN_SCHEMA,
        ),
    )
    return json.loads(response.text)


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section(title: str, inner_html: str) -> str:
    return (
        f'<div style="margin:0 0 24px;">'
        f'<h3 style="margin:0 0 10px;color:#0e2148;font-size:16px;font-weight:700;'
        f'border-bottom:2px solid #c9a227;padding-bottom:6px;">{_esc(title)}</h3>'
        f'{inner_html}</div>'
    )


def _list(items: list, ordered: bool = False) -> str:
    tag = "ol" if ordered else "ul"
    lis = "".join(f'<li style="margin:0 0 6px;">{_esc(i)}</li>' for i in items)
    return f'<{tag} style="margin:0 0 10px;padding-left:20px;line-height:1.6;color:#1f2937;">{lis}</{tag}>'


def _action_items(items: list) -> str:
    return (
        '<p style="margin:10px 0 0;font-weight:600;color:#0e2148;font-size:13px;">Action items:</p>'
        + _list(items, ordered=True)
    )


def _render_html(plan: dict, business_name: str) -> str:
    """Renders the structured plan into portable, inline-styled HTML - no
    external stylesheet or CSS classes, so the exact same string works
    embedded in an email body (see email_service.send_early_assets_email)
    and inside a normal page on the dashboard."""
    channels_html = "".join(
        f'<div style="margin:0 0 8px;"><strong style="color:#0e2148;">{_esc(c["channel"])}</strong>'
        f'<span style="color:#4b5563;"> — {_esc(c["rationale"])}</span></div>'
        for c in plan["top_channels"]
    )

    w1 = plan["week1_foundation"]
    week1_html = _section("Week 1 — Foundation", (
        f'<p style="margin:0 0 8px;color:#1f2937;"><strong>Google Business Profile:</strong> {_esc(w1["google_business_profile"])}</p>'
        f'<p style="margin:0 0 8px;color:#1f2937;"><strong>Social platforms:</strong> {_esc(", ".join(w1["social_platforms"]))}</p>'
        f'<p style="margin:0 0 8px;color:#1f2937;"><strong>Target audience:</strong> {_esc(w1["target_audience"])}</p>'
        + _action_items(w1["action_items"])
    ))

    w2 = plan["week2_content"]
    week2_html = _section("Week 2 — Content", (
        '<p style="margin:0 0 4px;font-weight:600;color:#0e2148;">Content calendar (5 post ideas):</p>'
        + _list(w2["content_calendar"])
        + '<p style="margin:10px 0 4px;font-weight:600;color:#0e2148;">Sample posts:</p>'
        + _list(w2["sample_posts"])
        + f'<p style="margin:10px 0 8px;color:#1f2937;"><strong>Hashtags:</strong> {_esc(" ".join("#" + h.lstrip("#") for h in w2["hashtags"]))}</p>'
        + _action_items(w2["action_items"])
    ))

    w3 = plan["week3_outreach"]
    week3_html = _section("Week 3 — Outreach", (
        '<p style="margin:0 0 4px;font-weight:600;color:#0e2148;">Local networking:</p>'
        + _list(w3["local_networking"])
        + '<p style="margin:10px 0 4px;font-weight:600;color:#0e2148;">Partnership ideas:</p>'
        + _list(w3["partnerships"])
        + '<p style="margin:10px 0 4px;font-weight:600;color:#0e2148;">Email outreach template:</p>'
        + f'<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:10px;white-space:pre-wrap;color:#1f2937;font-size:13px;">{_esc(w3["email_template"])}</div>'
        + _action_items(w3["action_items"])
    ))

    w4 = plan["week4_growth"]
    week4_html = _section("Week 4 — Growth", (
        f'<p style="margin:0 0 8px;color:#1f2937;"><strong>Paid advertising:</strong> {_esc(w4["paid_ads_recommendation"])}</p>'
        f'<p style="margin:0 0 8px;color:#1f2937;"><strong>Budget recommendation:</strong> {_esc(w4["budget_recommendation"])}</p>'
        '<p style="margin:10px 0 4px;font-weight:600;color:#0e2148;">Key metrics to track:</p>'
        + _list(w4["key_metrics"])
        + _action_items(w4["action_items"])
    ))

    budget_rows = "".join(
        f'<tr><td style="padding:6px 10px;border-bottom:1px solid #e5e7eb;color:#1f2937;">{_esc(b["category"])}</td>'
        f'<td style="padding:6px 10px;border-bottom:1px solid #e5e7eb;color:#0e2148;font-weight:600;text-align:right;">{_esc(b["amount"])}</td></tr>'
        for b in plan["budget_breakdown"]
    )
    budget_html = _section("Estimated Monthly Budget Breakdown", (
        f'<table style="width:100%;border-collapse:collapse;font-size:13px;">{budget_rows}</table>'
    ))

    goals_html = _section("90-Day Goals", _list(plan["goals_90_day"]))

    top_channels_html = _section("Top 3 Marketing Channels", channels_html)

    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937;">'
        f'<h2 style="margin:0 0 16px;color:#0e2148;font-size:20px;">30-Day Marketing Plan — {_esc(business_name)}</h2>'
        f'{top_channels_html}{week1_html}{week2_html}{week3_html}{week4_html}{budget_html}{goals_html}'
        f'</div>'
    )


def generate_marketing_plan(business_name: str, business_idea: str, target_customer: str,
                             industry: str, location: str = "Virginia") -> str:
    """Generates a complete 30-day marketing plan and returns it as
    self-contained, inline-styled HTML - ready to embed directly in an
    email body or a dashboard page. Raises on failure rather than
    returning a placeholder; callers (run_document_generation) already
    catch and record agent failures uniformly, same as brand kit/website."""
    plan = _generate_plan_data(business_name, business_idea, target_customer, industry, location)
    return _render_html(plan, business_name)
