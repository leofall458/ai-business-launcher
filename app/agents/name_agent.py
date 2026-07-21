import re

from app.agents import generate_content
from app.ai_ops import instrumented

MODEL = "gemini-2.5-flash"

_NAME_GEN_KWARGS = dict(
    model=MODEL, provider="vertex_ai", operation="generate",
    autonomy="autonomous", actor="agent", step_label="LLC Name Generation",
)

def screen_business_name(business_idea: str) -> dict:
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

    response = generate_content(
        model=MODEL,
        contents=prompt
    )

    return {
        "status": "complete",
        "result": response.text,
        "business_idea": business_idea,
        "state": "Virginia"
    }


@instrumented(
    "name_generation", **_NAME_GEN_KWARGS,
    input_summary_fn=lambda business_idea, count=5, *a, **k: f"business idea, requesting {count} names",
)
def generate_name_ideas(business_idea: str, count: int = 5) -> list[str]:
    """Generate fresh LLC name ideas for Step 3 of the dashboard flow, before
    the customer has tried (and had rejected) any specific name - unlike
    suggest_alternative_names below, there's nothing to be "distinct from"
    yet."""
    prompt = f"""You are a Virginia LLC naming expert.

Business idea: {business_idea}

Suggest exactly {count} strong Virginia LLC names for this business that:
1. Are distinct from each other — not just variations on the same word
2. Capture the business's essence or market niche
3. End with " LLC"
4. Are short (2-4 words before LLC) and memorable

Reply with ONLY a numbered list of {count} names, one per line. No explanations, no extra text.
Example format:
1. Blue Ridge Advisors LLC
2. Shenandoah Capital LLC
3. Potomac Strategy LLC
4. Cardinal Path LLC
5. Skyline Ventures LLC"""

    response = generate_content(model=MODEL, contents=prompt)
    names = []
    for m in re.finditer(r'\b([A-Z][A-Za-z0-9 &\'.\-]{1,40} LLC)\b', response.text):
        name = m.group(1).strip()
        if name not in names:
            names.append(name)
    return names[:count]


@instrumented(
    "name_generation", **_NAME_GEN_KWARGS,
    input_summary_fn=lambda taken_name, business_idea="", *a, **k: "alternative names after SCC rejection",
)
def suggest_alternative_names(taken_name: str, business_idea: str) -> list[str]:
    """Generate up to 5 LLC name alternatives when the requested name is taken."""
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

    response = generate_content(model=MODEL, contents=prompt)
    names = []
    for m in re.finditer(r'\b([A-Z][A-Za-z0-9 &\'.\-]{1,40} LLC)\b', response.text):
        name = m.group(1).strip()
        if name not in names and name.upper() != taken_name.upper():
            names.append(name)
    return names[:5]
