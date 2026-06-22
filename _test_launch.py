from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

form_data = {
    "first_name": "Leo",
    "middle_name": "",
    "last_name": "Fall",
    "email": "leo@example.com",
    "phone": "7035550100",
    "dob": "1990-01-01",
    "address": "2223 North Powhatan Street",
    "city": "Arlington",
    "county": "Arlington",
    "zipcode": "22205",
    "ssn": "348-04-6044",
    "business_idea": "A mobile bike repair service that comes to your home or office in Northern Virginia",
    "desired_name": "Spokewise Mobile Bike Repair LLC",
    "business_purpose": "On-demand mobile bicycle repair and tune-up services",
    "target_customer": "Commuter cyclists in Northern Virginia",
    "industry_code": "0",
    "duration": "Perpetual",
    "template_style": "auto",
    "sig_first": "Leo",
    "sig_middle": "",
    "sig_last": "Fall",
    "agree_terms": "on",
}
resp = client.post("/launch", data=form_data)
print("STATUS:", resp.status_code)
html = resp.text
import re
m = re.search(r'https://[a-z0-9-]+\.web\.app', html)
print("WEBSITE URL FOUND:", m.group(0) if m else None)
