from google import genai

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
