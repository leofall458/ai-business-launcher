from app.agents import get_client

MODEL = "gemini-2.5-flash"

def screen_business_name(business_idea: str) -> dict:
    client = get_client()

    prompt = f"""
    You are a business name expert helping a founder launch a Virginia LLC.
    
    Business idea: {business_idea}
    State: Virginia
    
    Generate 3 strong business name options for this idea.
    For each name provide:
    - The name itself
    - Why it works for a Virginia business
    - A .com domain suggestion
    - Whether it needs "LLC" appended for Virginia filing
    
    Also flag any obvious trademark concerns.
    Virginia-specific note: names must be distinguishable from existing Virginia entities.
    
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
        "state": "Virginia"
    }
