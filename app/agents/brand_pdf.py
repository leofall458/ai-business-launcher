"""Renders a structured brand kit dict (see app/agents/brand_agent.py) into a
multi-page PDF with ReportLab - same toolchain as app/agents/pdf_agent.py,
deliberately not WeasyPrint, since this app's Cloud Run image has none of
WeasyPrint's native system libraries (libpango/libcairo/etc) and ReportLab
is already proven here without any of that infra risk.

The AI-generated logo is real SVG markup, which ReportLab can't read
natively - svglib (pure-Python, no Cairo/Pango) converts it into a
reportlab.graphics Drawing instead. If Gemini's SVG fails to parse for any
reason, _monogram_drawing draws a simple vector circle-and-initial mark
instead, so a malformed logo never blocks PDF generation.
"""

import io
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle,
    PageBreak, Flowable,
)
from reportlab.graphics.shapes import Drawing, Circle, String
from reportlab.graphics import renderPDF

PAGE_W, PAGE_H = letter

def _monogram_drawing(business_name: str, hex_color: str, size: float = 1.4 * inch) -> Drawing:
    """Vector fallback brand mark - a colored circle with the business's
    first initial - used whenever the AI's SVG can't be parsed."""
    initial = (business_name or "?").strip()[:1].upper() or "?"
    d = Drawing(size, size)
    d.add(Circle(size / 2, size / 2, size / 2, fillColor=colors.HexColor(hex_color), strokeColor=None))
    d.add(String(size / 2, size / 2 - size * 0.16, initial, fontName="Helvetica-Bold",
                  fontSize=size * 0.45, fillColor=colors.white, textAnchor="middle"))
    return d

def _logo_drawing(logo_svg: str, business_name: str, primary_hex: str, size: float = 1.4 * inch) -> Drawing:
    if logo_svg:
        try:
            from svglib.svglib import svg2rlg
            drawing = svg2rlg(io.BytesIO(logo_svg.encode("utf-8")))
            if drawing is not None and drawing.width and drawing.height:
                scale = size / max(drawing.width, drawing.height)
                drawing.width *= scale
                drawing.height *= scale
                drawing.scale(scale, scale)
                return drawing
        except Exception as e:
            print(f"⚠️ Could not parse AI-generated logo SVG, using fallback monogram: {e}")
    return _monogram_drawing(business_name, primary_hex, size)

class _CenteredDrawing(Flowable):
    """Wraps a Drawing so platypus centers it on the page like a Paragraph,
    instead of always hugging the left margin."""
    def __init__(self, drawing: Drawing, page_width: float):
        super().__init__()
        self.drawing = drawing
        self.page_width = page_width
        self.width = page_width
        self.height = drawing.height

    def draw(self):
        x = (self.page_width - self.drawing.width) / 2
        renderPDF.draw(self.drawing, self.canv, x, 0)

def build_brand_kit_pdf(brand: dict, business_name: str, full_name: str = "",
                         email: str = "", phone: str = "", website_url: str = "") -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=0.85 * inch,
                             leftMargin=0.85 * inch, topMargin=0.85 * inch, bottomMargin=0.85 * inch)
    content_width = PAGE_W - doc.leftMargin - doc.rightMargin

    colors_data = brand.get("colors", {})
    fonts = brand.get("fonts", {})
    taglines = brand.get("taglines", [])
    voice = brand.get("voice", {})
    social = brand.get("social", {})
    logo_svg = brand.get("logo_svg", "")

    primary_hex = (colors_data.get("primary") or {}).get("hex", "#1E3A5F")
    text_hex = (colors_data.get("text") or {}).get("hex", "#1f2937")

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("BKTitle", parent=styles["Title"], fontSize=26, textColor=colors.HexColor(primary_hex), alignment=TA_CENTER)
    subtitle_style = ParagraphStyle("BKSubtitle", parent=styles["Normal"], fontSize=12, alignment=TA_CENTER, textColor=colors.grey, spaceAfter=6)
    section_title = ParagraphStyle("BKSection", parent=styles["Heading1"], fontSize=18, textColor=colors.HexColor(primary_hex), spaceAfter=14)
    heading_style = ParagraphStyle("BKHeading", parent=styles["Heading2"], fontSize=12, textColor=colors.HexColor(text_hex), spaceBefore=10, spaceAfter=4)
    body_style = ParagraphStyle("BKBody", parent=styles["Normal"], fontSize=10.5, leading=16, textColor=colors.HexColor(text_hex))
    small_style = ParagraphStyle("BKSmall", parent=styles["Normal"], fontSize=8.5, textColor=colors.grey)
    quote_style = ParagraphStyle("BKQuote", parent=styles["Normal"], fontSize=11, leading=15, textColor=colors.HexColor(text_hex), leftIndent=14, borderColor=colors.HexColor(primary_hex), borderWidth=0, alignment=TA_LEFT)

    story = []

    # ─── COVER PAGE ─────────────────────────────────────────────
    story.append(Spacer(1, 0.6 * inch))
    story.append(_CenteredDrawing(_logo_drawing(logo_svg, business_name, primary_hex), content_width))
    story.append(Spacer(1, 0.35 * inch))
    story.append(Paragraph(business_name or "Your Business", title_style))
    story.append(Paragraph("Brand Kit", subtitle_style))
    if taglines:
        story.append(Spacer(1, 0.15 * inch))
        story.append(Paragraph(f"“{taglines[0]}”", ParagraphStyle("BKTagline", parent=styles["Normal"], fontSize=13, alignment=TA_CENTER, textColor=colors.HexColor(primary_hex), fontName="Helvetica-Oblique")))
    story.append(Spacer(1, 0.8 * inch))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey))
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph("Prepared by Launch Bridge LLC", small_style))
    story.append(PageBreak())

    # ─── COLOR PALETTE ──────────────────────────────────────────
    story.append(Paragraph("Color Palette", section_title))
    story.append(Paragraph("These five colors are your brand's visual foundation - use them consistently across your website, social media, and printed materials.", body_style))
    story.append(Spacer(1, 0.25 * inch))
    role_order = ["primary", "secondary", "background", "text", "light"]
    role_labels = {"primary": "Primary", "secondary": "Secondary", "background": "Background", "text": "Text", "light": "Light / Neutral"}
    swatch_w = content_width / len(role_order)
    swatch_row, label_row = [], []
    for role in role_order:
        c = colors_data.get(role) or {}
        hex_code = c.get("hex", "#CCCCCC")
        name = c.get("name", "")
        cell = Table([[""]], colWidths=[swatch_w - 6], rowHeights=[0.85 * inch])
        cell.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(hex_code)),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
        ]))
        swatch_row.append(cell)
        label_row.append(Paragraph(f"<b>{role_labels[role]}</b><br/>{name}<br/><font color='grey'>{hex_code.upper()}</font>",
                                    ParagraphStyle("swatchLabel", parent=styles["Normal"], fontSize=8.5, alignment=TA_CENTER)))
    palette_table = Table([swatch_row, label_row], colWidths=[swatch_w] * len(role_order))
    palette_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 1), (-1, 1), 8)]))
    story.append(palette_table)
    story.append(PageBreak())

    # ─── TYPOGRAPHY ─────────────────────────────────────────────
    story.append(Paragraph("Typography", section_title))
    heading_font = fonts.get("heading", "Poppins")
    body_font = fonts.get("body", "Inter")
    story.append(Paragraph(
        f"Heading font: <b>{heading_font}</b> (Google Fonts) &nbsp;&nbsp;•&nbsp;&nbsp; Body font: <b>{body_font}</b> (Google Fonts)",
        body_style))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(fonts.get("pairing_rationale", ""), body_style))
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph(f"Style preview <font color='grey' size='8'>(approximate - your actual site renders the real Google Fonts above)</font>", small_style))
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph(business_name or "Your Business", ParagraphStyle("headingPreview", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=30, textColor=colors.HexColor(primary_hex))))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph("The quick brown fox jumps over the lazy dog. 0123456789", ParagraphStyle("bodyPreview", parent=styles["Normal"], fontName="Helvetica", fontSize=12, textColor=colors.HexColor(text_hex))))
    story.append(PageBreak())

    # ─── LOGO USAGE ─────────────────────────────────────────────
    story.append(Paragraph("Logo Usage", section_title))
    story.append(_CenteredDrawing(_logo_drawing(logo_svg, business_name, primary_hex, size=1.1 * inch), content_width))
    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph("Do", heading_style))
    for line in ["Keep clear space around the logo equal to at least the height of its mark.",
                 "Use the full-color version on light backgrounds and the white/mono version on dark or busy backgrounds.",
                 "Scale the logo proportionally - never stretch its width or height independently."]:
        story.append(Paragraph(f"✓ {line}", body_style))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph("Don't", heading_style))
    for line in ["Don't recolor the logo outside this brand kit's palette.",
                 "Don't add drop shadows, outlines, or other effects.",
                 "Don't place it on a background with insufficient contrast."]:
        story.append(Paragraph(f"✗ {line}", body_style))
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Your vector logo file (logo.svg) is available as a separate download on your dashboard, ready to hand to a printer or sign maker.", small_style))
    story.append(PageBreak())

    # ─── BRAND VOICE & TAGLINES ─────────────────────────────────
    story.append(Paragraph("Brand Voice & Taglines", section_title))
    story.append(Paragraph("Brand Story", heading_style))
    story.append(Paragraph(voice.get("story", ""), body_style))
    story.append(Paragraph("Tone", heading_style))
    story.append(Paragraph(voice.get("tone", ""), body_style))
    story.append(Paragraph("Core Values", heading_style))
    for v in voice.get("values", []):
        story.append(Paragraph(f"• {v}", body_style))
    story.append(Paragraph("Target Audience", heading_style))
    story.append(Paragraph(voice.get("audience", ""), body_style))
    story.append(Paragraph("Tagline Options", heading_style))
    for t in taglines:
        story.append(Paragraph(f"“{t}”", quote_style))
        story.append(Spacer(1, 0.05 * inch))
    story.append(PageBreak())

    # ─── SOCIAL MEDIA KIT ───────────────────────────────────────
    story.append(Paragraph("Social Media Kit", section_title))
    story.append(Paragraph("Profile Photo Style", heading_style))
    story.append(Paragraph(social.get("photo_style", ""), body_style))
    story.append(Paragraph("Sample Post Templates", heading_style))
    for p in social.get("post_templates", []):
        story.append(Paragraph(p, quote_style))
        story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph("Hashtag Suggestions", heading_style))
    hashtags = " ".join(h if h.startswith("#") else f"#{h}" for h in social.get("hashtags", []))
    story.append(Paragraph(hashtags, body_style))
    story.append(PageBreak())

    # ─── BUSINESS CARD MOCKUP ───────────────────────────────────
    story.append(Paragraph("Business Card", section_title))
    card_w, card_h = 3.5 * inch, 2.0 * inch
    card_data = [[Paragraph(
        f"<font color='white' size='13'><b>{business_name}</b></font><br/><br/>"
        f"<font color='white' size='9'>{full_name or 'Owner'}</font><br/>"
        f"<font color='white' size='8'>{email}</font><br/>"
        f"<font color='white' size='8'>{phone}</font><br/>"
        f"<font color='white' size='8'>{website_url}</font>",
        ParagraphStyle("cardText", parent=styles["Normal"], leading=13))]]
    card = Table(card_data, colWidths=[card_w], rowHeights=[card_h])
    card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(primary_hex)),
        ("LEFTPADDING", (0, 0), (-1, -1), 18),
        ("TOPPADDING", (0, 0), (-1, -1), 18),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
    ]))
    centered_card = Table([[card]], colWidths=[content_width])
    centered_card.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    story.append(centered_card)
    story.append(PageBreak())

    # ─── EMAIL SIGNATURE MOCKUP ──────────────────────────────────
    story.append(Paragraph("Email Signature", section_title))
    sig_data = [[
        "",
        Paragraph(
            f"<font size='11'><b>{full_name or 'Owner'}</b></font><br/>"
            f"<font size='9' color='{primary_hex}'>{business_name}</font><br/><br/>"
            f"<font size='8.5'>{phone}  |  {email}</font><br/>"
            f"<font size='8.5'>{website_url}</font>",
            ParagraphStyle("sigText", parent=styles["Normal"], leading=13)),
    ]]
    sig_table = Table(sig_data, colWidths=[0.15 * inch, 4 * inch], rowHeights=[1.0 * inch])
    sig_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor(primary_hex)),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 0),
        ("TOPPADDING", (1, 0), (1, 0), 8),
        ("LEFTPADDING", (1, 0), (1, 0), 12),
    ]))
    story.append(sig_table)
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph("Paste this layout into your email client's signature settings, matching the colors and fonts above.", small_style))

    doc.build(story)
    return buffer.getvalue()
