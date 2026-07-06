from app.agents import generate_content

MODEL = "gemini-2.5-flash"

def generate_llc_paperwork(business_name: str, owner_name: str, business_purpose: str, registered_agent: str, principal_address: str) -> dict:
    prompt = f"""
    You are a Virginia business formation expert. Generate a complete Virginia LLC formation package.
    
    Business Name: {business_name}
    Owner Full Name: {owner_name}
    Business Purpose: {business_purpose}
    Registered Agent Name: {registered_agent}
    Principal Office Address: {principal_address}
    State: Virginia
    
    Generate the following documents with the actual information filled in:

    === DOCUMENT 1: ARTICLES OF ORGANIZATION ===
    Generate a complete Virginia Articles of Organization form filled out with the above information.
    Virginia requires these exact sections:
    - Entity Name (must include LLC)
    - Registered Agent name and address
    - Principal office address
    - Organizer name and signature line
    - Effective date
    
    === DOCUMENT 2: OPERATING AGREEMENT ===
    Generate a complete single-member LLC Operating Agreement for Virginia with:
    - Company name and formation date
    - Member name and 100% ownership
    - Management structure
    - Capital contributions
    - Profit and loss allocation
    - Dissolution terms
    - Governing law (Virginia)
    
    === DOCUMENT 3: FILING INSTRUCTIONS ===
    Exact step-by-step instructions to file with Virginia SCC:
    - File online at https://cis.scc.virginia.gov
    - Current filing fee: $100
    - Processing time
    - What to expect after filing
    - How to get certified copies
    
    === DOCUMENT 4: POST-FILING CHECKLIST ===
    What to do immediately after the LLC is approved:
    - Get EIN from IRS
    - Open business bank account
    - Set up business accounting
    - Get required licenses
    - File annual registration (due by the last day of the 12th month after formation)
    
    Use the actual business name and owner name throughout all documents.
    """

    response = generate_content(
        model=MODEL,
        contents=prompt
    )

    return {
        "status": "complete",
        "result": response.text,
        "business_name": business_name,
        "owner_name": owner_name,
        "state": "Virginia"
    }
