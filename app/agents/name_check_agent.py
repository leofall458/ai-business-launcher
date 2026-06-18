import httpx
from app.agents import get_client

MODEL = "gemini-2.5-flash"

def check_domain_available(domain: str) -> bool:
    try:
        response = httpx.get(f"https://dns.google/resolve?name={domain}&type=A", timeout=5)
        data = response.json()
        return data.get("Status") == 3 or "Answer" not in data
    except:
        return None

def check_business_name(desired_name: str, state: str = "Virginia") -> dict:
    client = get_client()

    clean_name = desired_name.lower().replace(" ", "").replace(",", "").replace(".", "").replace("'", "")
    domain = f"{clean_name}.com"
    domain_available = check_domain_available(domain)

    prompt = f"""
    You are a business name availability expert for Virginia LLCs.
    
    Proposed Name: "{desired_name}"
    State: Virginia
    
    Provide:
    1. Trademark Risk Assessment — is this name too similar to well-known brands? Rate LOW/MEDIUM/HIGH risk
    2. Name Strength Score — rate 1-10 for memorability, uniqueness, and professionalism
    3. Potential Conflicts — list any well-known businesses with similar names
    4. Virginia-Specific Notes — does it need LLC appended? Any prohibited words?
    5. Name Variations to Also Check — 3 similar names they should search on the SCC website
    6. Overall Recommendation — PROCEED, PROCEED WITH CAUTION, or CHOOSE DIFFERENT NAME
    
    Be honest and specific.
    """

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt
    )

    # Direct link to Virginia SCC name availability checker
    scc_url = f"https://cis.scc.virginia.gov/Account/NameCheckAvailability"

    return {
        "status": "complete",
        "desired_name": desired_name,
        "domain": domain,
        "domain_available": domain_available,
        "gemini_analysis": response.text,
        "scc_url": scc_url
    }
