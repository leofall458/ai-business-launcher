from app.agents import get_client

MODEL = "gemini-2.5-flash"

def screen_business_name(business_idea: str, state: str) -> dict:
    client = get_client()

    prompt = f"""
    You are a business name expert helping a founder launch an LLC.
    
    Business idea: {business_idea}
    State: {state}
    
    Generate 3 strong business name options for this idea.
    For each name provide:
    - The name itself
    - Why it works
    - A .com domain suggestion
    
    Also flag any obvious trademark concerns.
    
    Format your response clearly with numbered options.
    """

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt
    )

    return {
        "status": "complete",
        "result": response.text,
        "business_idea": business_idea,
        "state": state
    }
