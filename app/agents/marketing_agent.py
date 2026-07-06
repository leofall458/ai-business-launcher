import io
import json
from google.genai import types
from app.agents import get_client
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle, PageBreak

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
        f'<h2 style="margin:0 0 16px;color:#0e2148;font-size:20px;">Starter Marketing Plan — {_esc(business_name)}</h2>'
        f'{top_channels_html}{week1_html}{week2_html}{week3_html}{week4_html}{budget_html}{goals_html}'
        f'</div>'
    )


def generate_marketing_plan(business_name: str, business_idea: str, target_customer: str,
                             industry: str, location: str = "Virginia") -> dict:
    """Generates a complete Starter Marketing Plan. Returns {"html": ...,
    "plan": ...} rather than just the rendered HTML string - "plan" (the
    raw structured data) is what build_marketing_plan_pdf needs to produce
    a downloadable file the same way brand_pdf.py does for the brand kit;
    re-deriving it from the HTML would be fragile. Raises on failure
    rather than returning a placeholder; callers (run_document_generation)
    already catch and record agent failures uniformly, same as brand
    kit/website."""
    plan = _generate_plan_data(business_name, business_idea, target_customer, industry, location)
    return {"html": _render_html(plan, business_name), "plan": plan}


def build_marketing_plan_pdf(plan: dict, business_name: str) -> bytes:
    """Renders the structured plan dict (see MARKETING_PLAN_SCHEMA) into a
    downloadable PDF with ReportLab - same toolchain as brand_pdf.py, for
    the same reason (this app's Cloud Run image has none of WeasyPrint's
    native system libraries)."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=0.85 * inch,
                             leftMargin=0.85 * inch, topMargin=0.85 * inch, bottomMargin=0.85 * inch)
    content_width = letter[0] - doc.leftMargin - doc.rightMargin
    primary_hex, text_hex = "#0e2148", "#1f2937"

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("MPTitle", parent=styles["Title"], fontSize=22, textColor=colors.HexColor(primary_hex), alignment=TA_CENTER)
    subtitle_style = ParagraphStyle("MPSubtitle", parent=styles["Normal"], fontSize=12, alignment=TA_CENTER, textColor=colors.grey, spaceAfter=6)
    section_style = ParagraphStyle("MPSection", parent=styles["Heading1"], fontSize=16, textColor=colors.HexColor(primary_hex), spaceBefore=14, spaceAfter=8)
    heading_style = ParagraphStyle("MPHeading", parent=styles["Heading2"], fontSize=11, textColor=colors.HexColor(text_hex), spaceBefore=8, spaceAfter=3)
    body_style = ParagraphStyle("MPBody", parent=styles["Normal"], fontSize=10, leading=15, textColor=colors.HexColor(text_hex))
    small_style = ParagraphStyle("MPSmall", parent=styles["Normal"], fontSize=8.5, textColor=colors.grey)

    def section(title):
        story.append(Paragraph(title, section_style))

    def heading(title):
        story.append(Paragraph(title, heading_style))

    def body(text):
        story.append(Paragraph(text, body_style))

    def bullets(items, ordered=False):
        prefix = lambda i: f"{i + 1}." if ordered else "•"
        for i, item in enumerate(items):
            story.append(Paragraph(f"{prefix(i)} {item}", body_style))

    def action_items(items):
        heading("Action items")
        bullets(items, ordered=True)

    story = []

    # ─── COVER ────────────────────────────────────────────────
    story.append(Spacer(1, 1.2 * inch))
    story.append(Paragraph(business_name or "Your Business", title_style))
    story.append(Paragraph("Starter Marketing Plan", subtitle_style))
    story.append(Spacer(1, 0.6 * inch))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey))
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph("Prepared by Launch Bridge LLC", small_style))
    story.append(PageBreak())

    # ─── TOP CHANNELS ─────────────────────────────────────────
    section("Top 3 Marketing Channels")
    for c in plan["top_channels"]:
        story.append(Paragraph(f"<b>{c['channel']}</b> — {c['rationale']}", body_style))
        story.append(Spacer(1, 0.05 * inch))

    # ─── WEEK 1 ───────────────────────────────────────────────
    w1 = plan["week1_foundation"]
    section("Week 1 — Foundation")
    body(f"<b>Google Business Profile:</b> {w1['google_business_profile']}")
    body(f"<b>Social platforms:</b> {', '.join(w1['social_platforms'])}")
    body(f"<b>Target audience:</b> {w1['target_audience']}")
    action_items(w1["action_items"])

    # ─── WEEK 2 ───────────────────────────────────────────────
    w2 = plan["week2_content"]
    section("Week 2 — Content")
    heading("Content calendar (5 post ideas)")
    bullets(w2["content_calendar"])
    heading("Sample posts")
    bullets(w2["sample_posts"])
    body(f"<b>Hashtags:</b> {' '.join('#' + h.lstrip('#') for h in w2['hashtags'])}")
    action_items(w2["action_items"])

    # ─── WEEK 3 ───────────────────────────────────────────────
    w3 = plan["week3_outreach"]
    section("Week 3 — Outreach")
    heading("Local networking")
    bullets(w3["local_networking"])
    heading("Partnership ideas")
    bullets(w3["partnerships"])
    heading("Email outreach template")
    body(w3["email_template"].replace("\n", "<br/>"))
    action_items(w3["action_items"])

    # ─── WEEK 4 ───────────────────────────────────────────────
    w4 = plan["week4_growth"]
    section("Week 4 — Growth")
    body(f"<b>Paid advertising:</b> {w4['paid_ads_recommendation']}")
    body(f"<b>Budget recommendation:</b> {w4['budget_recommendation']}")
    heading("Key metrics to track")
    bullets(w4["key_metrics"])
    action_items(w4["action_items"])

    # ─── BUDGET BREAKDOWN ─────────────────────────────────────
    section("Estimated Monthly Budget Breakdown")
    rows = [[b["category"], b["amount"]] for b in plan["budget_breakdown"]]
    budget_table = Table(rows, colWidths=[content_width * 0.7, content_width * 0.3])
    budget_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor(text_hex)),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(budget_table)

    # ─── 90-DAY GOALS ─────────────────────────────────────────
    section("90-Day Goals")
    bullets(plan["goals_90_day"])

    doc.build(story)
    return buffer.getvalue()
