import os
from google import genai

client = genai.Client(
    vertexai=True,
    project="ai-biz-launcher",
    location="us-central1"
)

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Say hello and confirm you are working."
)

print(response.text)
