import re

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


def suggest_alternative_names(taken_name: str, business_idea: str) -> list[str]:
    """Generate up to 5 LLC name alternatives when the requested name is taken."""
    client = get_client()
    context = f'Business idea: {business_idea}' if business_idea else f'Similar concept to "{taken_name}"'
    prompt = f"""You are a Virginia LLC naming expert.

The customer tried to register "{taken_name}" but it is already taken in Virginia.
{context}

Suggest exactly 5 alternative Virginia LLC names that:
1. Are clearly distinct from "{taken_name}" — not just adding "Pro", "Plus", "Group", or "Solutions"
2. Capture the same business essence or market niche
3. End with " LLC"
4. Are short (2-4 words before LLC) and memorable

Reply with ONLY a numbered list of 5 names, one per line. No explanations, no extra text.
Example format:
1. Blue Ridge Advisors LLC
2. Shenandoah Capital LLC
3. Potomac Strategy LLC
4. Cardinal Path LLC
5. Skyline Ventures LLC"""

    response = client.models.generate_content(model=MODEL, contents=prompt)
    names = []
    for m in re.finditer(r'\b([A-Z][A-Za-z0-9 &\'.\-]{1,40} LLC)\b', response.text):
        name = m.group(1).strip()
        if name not in names and name.upper() != taken_name.upper():
            names.append(name)
    return names[:5]
