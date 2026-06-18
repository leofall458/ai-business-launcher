from app.agents import get_client

MODEL = "gemini-2.5-flash"

def generate_brand_kit(business_name: str, business_idea: str, target_customer: str) -> dict:
    client = get_client()

    prompt = f"""
    You are a brand designer creating a complete brand identity for a new Virginia business.
    
    Business Name: {business_name}
    Business Idea: {business_idea}
    Target Customer: {target_customer}
    
    Create a complete brand kit including:
    1. Brand Story — 2-3 sentences that capture the mission and personality
    2. Tagline — 3 options, punchy and memorable
    3. Color Palette — primary, secondary, accent colors with hex codes and why each was chosen
    4. Typography — recommended font pairings (Google Fonts) for headings and body text
    5. Brand Voice — 5 adjectives that describe how the brand communicates
    6. Logo Concept — detailed text description of a logo concept a designer could execute
    7. Social Media Handle Suggestions — 3 options that work across platforms
    
    Make everything cohesive and professional.
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
