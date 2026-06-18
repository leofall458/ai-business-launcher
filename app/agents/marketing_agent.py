from app.agents import get_client

MODEL = "gemini-2.5-flash"

def generate_marketing_plan(business_name: str, business_idea: str, state: str, target_customer: str) -> dict:
    client = get_client()

    prompt = f"""
    You are a marketing strategist creating a 30-day launch plan for a new Virginia business.
    
    Business Name: {business_name}
    Business: {business_idea}
    Location: {state}
    Target Customer: {target_customer}
    
    Create a complete 30-day marketing plan including:
    1. Top 3 marketing channels for this specific business with rationale
    2. Week 1 actions — exact tasks to do each day
    3. Week 2-4 actions — weekly goals and key activities
    4. First 10 customers strategy — exactly how to get them
    5. Social media content — 5 ready-to-post ideas with captions
    6. Local Virginia outreach — specific organizations, groups, or businesses to contact
    7. Budget breakdown — how to spend first $500 on marketing
    
    Be specific and actionable — no generic advice.
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
