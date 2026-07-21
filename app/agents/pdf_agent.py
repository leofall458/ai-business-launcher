import io
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from app.ai_ops import instrumented

@instrumented(
    "document_assembly", model=None, provider="none", operation="assemble",
    autonomy="autonomous", actor="agent", step_label="LLC Document Assembly",
    # owner_name/principal_address/signature are PII and are literal
    # arguments of this function - fixed string only, nothing derived from args.
    input_summary_fn=lambda *a, **k: "Articles of Organization + Operating Agreement",
)
def generate_llc_pdf(
    business_name: str,
    owner_name: str,
    business_purpose: str,
    registered_agent: str,
    principal_address: str,
    signature: str
) -> bytes:
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=inch,
        leftMargin=inch,
        topMargin=inch,
        bottomMargin=inch
    )

    styles = getSampleStyleSheet()
    story = []

    # Custom styles
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Title'],
        fontSize=18,
        spaceAfter=6,
        textColor=colors.HexColor('#1a1a2e')
    )
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading1'],
        fontSize=13,
        spaceAfter=6,
        spaceBefore=16,
        textColor=colors.HexColor('#16213e'),
        borderPad=4,
    )
    body_style = ParagraphStyle(
        'CustomBody',
        parent=styles['Normal'],
        fontSize=10,
        spaceAfter=6,
        leading=16
    )
    small_style = ParagraphStyle(
        'Small',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.grey
    )

    date_str = datetime.now().strftime("%B %d, %Y")

    # ─── COVER PAGE ───────────────────────────────────────────
    story.append(Spacer(1, 0.5*inch))
    story.append(Paragraph("COMMONWEALTH OF VIRGINIA", ParagraphStyle('center', parent=styles['Normal'], alignment=TA_CENTER, fontSize=11, textColor=colors.grey)))
    story.append(Spacer(1, 0.1*inch))
    story.append(Paragraph("LLC FORMATION PACKAGE", title_style))
    story.append(Spacer(1, 0.1*inch))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#16213e')))
    story.append(Spacer(1, 0.3*inch))

    # Info table
    info_data = [
        ["Business Name:", business_name],
        ["Owner:", owner_name],
        ["State:", "Virginia"],
        ["Date Prepared:", date_str],
        ["Business Purpose:", business_purpose],
    ]
    info_table = Table(info_data, colWidths=[2*inch, 4*inch])
    info_table.setStyle(TableStyle([
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 0.4*inch))
    story.append(Paragraph("Prepared by AI Business Launcher · aibiznlauncher.com", small_style))
    story.append(Spacer(1, 0.8*inch))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey))
    story.append(Spacer(1, 0.3*inch))
    story.append(Paragraph("CONTENTS", ParagraphStyle('contents_title', parent=styles['Normal'], fontSize=11, fontName='Helvetica-Bold')))
    story.append(Spacer(1, 0.1*inch))
    for i, item in enumerate([
        "Document 1 — Articles of Organization",
        "Document 2 — Operating Agreement",
        "Document 3 — Virginia SCC Filing Instructions",
        "Document 4 — Post-Filing Checklist",
    ], 1):
        story.append(Paragraph(f"  {item}", body_style))
    story.append(Spacer(1, inch))

    # ─── DOCUMENT 1: ARTICLES OF ORGANIZATION ─────────────────
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#16213e')))
    story.append(Spacer(1, 0.2*inch))
    story.append(Paragraph("DOCUMENT 1", ParagraphStyle('doc_label', parent=styles['Normal'], fontSize=9, textColor=colors.grey, alignment=TA_CENTER)))
    story.append(Paragraph("ARTICLES OF ORGANIZATION", ParagraphStyle('doc_title', parent=styles['Title'], fontSize=16, alignment=TA_CENTER)))
    story.append(Paragraph("Limited Liability Company", ParagraphStyle('doc_sub', parent=styles['Normal'], fontSize=11, alignment=TA_CENTER, textColor=colors.grey)))
    story.append(Paragraph("Commonwealth of Virginia", ParagraphStyle('doc_sub2', parent=styles['Normal'], fontSize=11, alignment=TA_CENTER, textColor=colors.grey)))
    story.append(Spacer(1, 0.3*inch))
    story.append(Paragraph("Pursuant to Title 13.1, Chapter 12 of the Code of Virginia, the undersigned hereby sets forth the following Articles of Organization:", body_style))
    story.append(Spacer(1, 0.2*inch))

    articles = [
        ("ARTICLE I — NAME", f"The name of the limited liability company is <b>{business_name}</b>."),
        ("ARTICLE II — REGISTERED AGENT", f"The name of the registered agent is <b>{registered_agent}</b>. The registered agent is a member/manager of the LLC and their address is <b>{principal_address}</b>."),
        ("ARTICLE III — PRINCIPAL OFFICE", f"The address of the principal office of the limited liability company is <b>{principal_address}</b>."),
        ("ARTICLE IV — PURPOSE", f"The purpose of the limited liability company is to engage in {business_purpose} and any other lawful business activity permitted under the laws of the Commonwealth of Virginia."),
        ("ARTICLE V — MANAGEMENT", f"The limited liability company shall be managed by its member(s). The sole member is <b>{owner_name}</b>."),
        ("ARTICLE VI — ORGANIZER", f"The name and address of the organizer is <b>{owner_name}</b>, {principal_address}."),
        ("ARTICLE VII — EFFECTIVE DATE", f"These Articles of Organization shall be effective upon filing with the Virginia State Corporation Commission."),
    ]

    for title, content in articles:
        story.append(Paragraph(title, heading_style))
        story.append(Paragraph(content, body_style))

    # Signature block
    story.append(Spacer(1, 0.4*inch))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey))
    story.append(Spacer(1, 0.2*inch))
    story.append(Paragraph("ORGANIZER SIGNATURE", ParagraphStyle('sig_label', parent=styles['Normal'], fontSize=9, textColor=colors.grey)))
    story.append(Spacer(1, 0.1*inch))

    sig_data = [
        ["Signature:", signature],
        ["Printed Name:", owner_name],
        ["Date:", date_str],
        ["Capacity:", "Organizer / Sole Member"],
    ]
    sig_table = Table(sig_data, colWidths=[1.5*inch, 4.5*inch])
    sig_table.setStyle(TableStyle([
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('FONTNAME', (1,0), (1,0), 'Helvetica-Oblique'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LINEBELOW', (1,0), (1,0), 0.5, colors.black),
    ]))
    story.append(sig_table)
    story.append(Spacer(1, 0.5*inch))

    # ─── DOCUMENT 2: OPERATING AGREEMENT ──────────────────────
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#16213e')))
    story.append(Spacer(1, 0.2*inch))
    story.append(Paragraph("DOCUMENT 2", ParagraphStyle('doc_label', parent=styles['Normal'], fontSize=9, textColor=colors.grey, alignment=TA_CENTER)))
    story.append(Paragraph("OPERATING AGREEMENT", ParagraphStyle('doc_title', parent=styles['Title'], fontSize=16, alignment=TA_CENTER)))
    story.append(Paragraph(business_name, ParagraphStyle('doc_sub', parent=styles['Normal'], fontSize=12, alignment=TA_CENTER, textColor=colors.grey)))
    story.append(Spacer(1, 0.2*inch))
    story.append(Paragraph(f"This Operating Agreement (\"Agreement\") is entered into as of {date_str}, by and between {business_name} (the \"Company\") and {owner_name} (the \"Member\").", body_style))
    story.append(Spacer(1, 0.1*inch))

    sections = [
        ("SECTION 1 — FORMATION", f"The Member formed a Virginia limited liability company named <b>{business_name}</b> by filing Articles of Organization with the Virginia State Corporation Commission pursuant to the Virginia Limited Liability Company Act (Title 13.1, Chapter 12 of the Code of Virginia)."),
        ("SECTION 2 — PRINCIPAL OFFICE", f"The principal office of the Company shall be located at {principal_address}, or at such other place as the Member may designate from time to time."),
        ("SECTION 3 — PURPOSE", f"The purpose of the Company is to engage in {business_purpose} and any other lawful business or activity for which a limited liability company may be organized under the laws of the Commonwealth of Virginia."),
        ("SECTION 4 — MEMBERSHIP", f"<b>{owner_name}</b> is the sole Member of the Company and holds 100% membership interest. The Member's liability shall be limited as provided by Virginia law."),
        ("SECTION 5 — MANAGEMENT", f"The Company shall be member-managed. {owner_name}, as the sole Member, shall have full authority to manage the business and affairs of the Company, make all decisions, and bind the Company in contracts and agreements."),
        ("SECTION 6 — CAPITAL CONTRIBUTIONS", f"The Member has contributed capital to the Company as agreed upon. Additional capital contributions may be made at the Member's discretion."),
        ("SECTION 7 — PROFITS AND LOSSES", "All profits and losses of the Company shall be allocated entirely to the Member. Distributions shall be made at such times and in such amounts as the Member determines."),
        ("SECTION 8 — BANKING", f"The Company shall maintain one or more bank accounts in the name of {business_name}. The Member is authorized to open, maintain, and close such accounts."),
        ("SECTION 9 — TAXES", "The Company shall be treated as a disregarded entity for federal income tax purposes (sole proprietorship taxation). The Member shall report all income and losses on Schedule C of their personal federal income tax return."),
        ("SECTION 10 — DISSOLUTION", f"The Company shall be dissolved upon the written decision of the Member, or as otherwise required by Virginia law. Upon dissolution, assets shall be distributed to {owner_name} after payment of all debts and liabilities."),
        ("SECTION 11 — GOVERNING LAW", "This Agreement shall be governed by and construed in accordance with the laws of the Commonwealth of Virginia."),
        ("SECTION 12 — AMENDMENTS", "This Agreement may be amended only by a written instrument signed by the Member."),
    ]

    for title, content in sections:
        story.append(Paragraph(title, heading_style))
        story.append(Paragraph(content, body_style))

    # Operating Agreement Signature
    story.append(Spacer(1, 0.4*inch))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey))
    story.append(Spacer(1, 0.2*inch))
    story.append(Paragraph("MEMBER SIGNATURE", ParagraphStyle('sig_label', parent=styles['Normal'], fontSize=9, textColor=colors.grey)))
    story.append(Spacer(1, 0.1*inch))
    sig_data2 = [
        ["Signature:", signature],
        ["Printed Name:", owner_name],
        ["Title:", "Sole Member"],
        ["Date:", date_str],
    ]
    sig_table2 = Table(sig_data2, colWidths=[1.5*inch, 4.5*inch])
    sig_table2.setStyle(TableStyle([
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('FONTNAME', (1,0), (1,0), 'Helvetica-Oblique'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LINEBELOW', (1,0), (1,0), 0.5, colors.black),
    ]))
    story.append(sig_table2)
    story.append(Spacer(1, 0.5*inch))

    # ─── DOCUMENT 3: FILING INSTRUCTIONS ──────────────────────
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#16213e')))
    story.append(Spacer(1, 0.2*inch))
    story.append(Paragraph("DOCUMENT 3", ParagraphStyle('doc_label', parent=styles['Normal'], fontSize=9, textColor=colors.grey, alignment=TA_CENTER)))
    story.append(Paragraph("VIRGINIA SCC FILING INSTRUCTIONS", ParagraphStyle('doc_title', parent=styles['Title'], fontSize=16, alignment=TA_CENTER)))
    story.append(Spacer(1, 0.2*inch))

    steps = [
        ("Step 1 — Go to Virginia SCC", "Visit https://cis.scc.virginia.gov and click 'New Filing' then 'Articles of Organization — LLC'."),
        ("Step 2 — Enter Business Name", f"Enter exactly: {business_name}"),
        ("Step 3 — Enter Registered Agent", f"Enter: {registered_agent}, Address: {principal_address}"),
        ("Step 4 — Enter Principal Office", f"Enter: {principal_address}"),
        ("Step 5 — Enter Organizer", f"Enter: {owner_name}, {principal_address}"),
        ("Step 6 — Pay Filing Fee", "Pay $100 by credit/debit card. This is the Virginia state fee."),
        ("Step 7 — Submit and Save", "Submit the filing and save your confirmation number. Processing takes 1-3 business days."),
        ("Step 8 — Download Certificate", "Once approved, download your Certificate of Organization from the SCC portal. Keep this permanently."),
    ]

    for title, content in steps:
        story.append(Paragraph(title, heading_style))
        story.append(Paragraph(content, body_style))

    story.append(Spacer(1, 0.3*inch))
    story.append(Paragraph("⚠️ IMPORTANT: The $100 filing fee is paid directly to the Commonwealth of Virginia — it is not part of our service fee.", ParagraphStyle('warning', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#b45309'), backColor=colors.HexColor('#fef3c7'), borderPad=6)))

    # ─── DOCUMENT 4: POST-FILING CHECKLIST ────────────────────
    story.append(Spacer(1, 0.4*inch))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#16213e')))
    story.append(Spacer(1, 0.2*inch))
    story.append(Paragraph("DOCUMENT 4", ParagraphStyle('doc_label', parent=styles['Normal'], fontSize=9, textColor=colors.grey, alignment=TA_CENTER)))
    story.append(Paragraph("POST-FILING CHECKLIST", ParagraphStyle('doc_title', parent=styles['Title'], fontSize=16, alignment=TA_CENTER)))
    story.append(Spacer(1, 0.2*inch))

    checklist = [
        ("☐ Get Your EIN", "Apply at IRS.gov/EIN — free, takes 10 minutes online. You need this before opening a bank account."),
        ("☐ Open a Business Bank Account", f"Bring your Certificate of Organization and EIN to a bank. Open an account in the name of {business_name}."),
        ("☐ Set Up Business Accounting", "Use QuickBooks, Wave (free), or FreshBooks to track income and expenses from day one."),
        ("☐ Get Required Licenses", "Check Virginia's business license requirements for your specific industry at townhall.virginia.gov."),
        ("☐ Set Up Stripe", "Create a Stripe account at stripe.com to accept payments online and in person."),
        ("☐ File Annual Registration", f"Virginia requires an annual registration fee each year. Due by the last day of the 12th month after {business_name} was formed."),
        ("☐ Get Business Insurance", "At minimum, get general liability insurance. Required for some industries."),
        ("☐ Create an Operating Agreement", "Keep your signed Operating Agreement in a safe place — banks and investors may request it."),
    ]

    for title, content in checklist:
        story.append(Paragraph(title, heading_style))
        story.append(Paragraph(content, body_style))

    story.append(Spacer(1, 0.5*inch))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey))
    story.append(Spacer(1, 0.2*inch))
    story.append(Paragraph(f"Generated by AI Business Launcher on {date_str} · Not legal advice · Consult an attorney for complex situations.", small_style))

    doc.build(story)
    return buffer.getvalue()
