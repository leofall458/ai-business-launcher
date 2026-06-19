import json
import time
import requests

def ping_scc():
    """Ping SCC dashboard to keep session alive"""
    with open("scc_session.json", "r") as f:
        cookies = json.load(f)
    
    # Convert cookies to requests format
    session = requests.Session()
    for cookie in cookies:
        session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])
    
    response = session.get("https://cis.scc.virginia.gov/OnlineDashboard/Index")
    
    if "Hi," in response.text:
        print(f"✅ Session alive - {time.strftime('%H:%M:%S')}")
        return True
    else:
        print(f"❌ Session expired - {time.strftime('%H:%M:%S')}")
        return False

print("🔄 Keeping SCC session alive - press Ctrl+C to stop")
print("Run this in a separate terminal while filing LLCs\n")

while True:
    alive = ping_scc()
    if not alive:
        print("⚠️  Please run save_scc_session.py to refresh the session")
        break
    time.sleep(180)  # Ping every 3 minutes
