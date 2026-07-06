import time

from google import genai
from google.genai import errors as genai_errors

client = None

def get_client():
    global client
    if client is None:
        client = genai.Client(
            vertexai=True,
            project="ai-biz-launcher",
            location="us-central1"
        )
    return client

def generate_content(model: str, contents, config=None, max_attempts: int = 4):
    """Thin wrapper around client.models.generate_content with exponential
    backoff on 429 RESOURCE_EXHAUSTED - every agent (name, brand, website,
    marketing plan, LLC docs, EIN) should call this instead of the raw
    client method.

    A single order's asset generation fires several of these calls back
    to back (see run_document_generation), which can transiently exceed
    the project's per-minute Gemini quota even though the account is
    nowhere near any daily/overall limit - confirmed by hand: a call that
    got a 429 succeeded again just a couple of minutes later with no
    other change. Retrying beats letting one quota blip permanently fail
    a customer's order (previously: straight to assets_status=="failed",
    stuck until an admin noticed and clicked "Retry Assets").

    Only retries 429s - any other APIError (bad request, auth, etc.) is
    never transient and should surface immediately."""
    kwargs = {"model": model, "contents": contents}
    if config is not None:
        kwargs["config"] = config
    client = get_client()
    for attempt in range(max_attempts):
        try:
            return client.models.generate_content(**kwargs)
        except genai_errors.APIError as e:
            if e.code != 429 or attempt == max_attempts - 1:
                raise
            time.sleep(2 ** attempt * 2)  # 2s, 4s, 8s
