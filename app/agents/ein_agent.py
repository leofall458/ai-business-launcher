from app.agents import get_client

MODEL = "gemini-2.5-flash"

def generate_ein_guidance(business_name: str, owner_name: str, state: str) -> dict:
    client = get_client()

    prompt = f"""
    You are a tax expert helping a new Virginia LLC owner get their EIN.
    
    Business Name: {business_name}
    Owner: {owner_name}
    State: {state}
    
    Provide step-by-step instructions to apply for an EIN online at IRS.gov including:
    1. Exact steps to navigate to the EIN application
    2. Field-by-field guidance for a single-member Virginia LLC
    3. What to do immediately after receiving the EIN
    4. How to use the EIN to open a business bank account
    5. Common mistakes to avoid
    
    Be specific with field-by-field guidance for the SS-4 form.
    """

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt
    )

    return {
        "status": "complete",
        "result": response.text,
        "business_name": business_name
    }
