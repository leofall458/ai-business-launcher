import re

import httpx
from playwright.sync_api import sync_playwright
from app.secrets import get_secret

SCC_USERNAME = get_secret("SCC_USERNAME")
SCC_PASSWORD = get_secret("SCC_PASSWORD")

SCC_NAME_CHECK_URL = "https://cis.scc.virginia.gov/Account/NameCheckAvailability"
SCC_ENTITY_SEARCH_URL = "https://cis.scc.virginia.gov/EntitySearch/Index"

_SCC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ── HTML parsing helpers ────────────────────────────────────────────────────

def _extract_inputs(html: str) -> dict:
    """Return {name: value} for all <input> tags (excluding radio/checkbox)."""
    out = {}
    for m in re.finditer(r'<input\b([^>]*)>', html, re.IGNORECASE | re.DOTALL):
        seg = m.group(1)
        t = re.search(r'\btype\s*=\s*["\'](\w+)["\']', seg, re.IGNORECASE)
        if t and t.group(1).lower() in ("radio", "checkbox"):
            continue
        n = re.search(r'\bname\s*=\s*["\']([^"\']+)["\']', seg)
        v = re.search(r'\bvalue\s*=\s*["\']([^"\']*)["\']', seg)
        if n:
            out[n.group(1)] = v.group(1) if v else ""
    return out

def _find_name_by_id_suffix(html: str, id_suffix: str) -> str | None:
    """Return the name= attribute of the element whose id= ends with id_suffix."""
    for pat in [
        rf'<\w+[^>]*\bid\s*=\s*["\'][^"\']*{re.escape(id_suffix)}["\'][^>]*\bname\s*=\s*["\']([^"\']+)["\']',
        rf'<\w+[^>]*\bname\s*=\s*["\']([^"\']+)["\'][^>]*\bid\s*=\s*["\'][^"\']*{re.escape(id_suffix)}["\']',
    ]:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1)
    return None

def _parse_entity_names(html: str) -> list:
    """Extract entity name strings from the SCC results table."""
    names = []
    for m in re.finditer(
        r'<td[^>]*>\s*([A-Z][A-Z0-9 &\',./-]{2,60}(?:LLC|INC|CORP|LTD|LP|LLP)\.?)\s*</td>',
        html, re.IGNORECASE,
    ):
        name = m.group(1).strip()
        if name not in names:
            names.append(name)
    return names[:5]

# ── Public HTTP search (no Chrome / no login) ───────────────────────────────

def check_name_public(business_name: str) -> dict:
    """Search the SCC public entity database via plain HTTP — no Chrome or login.
    Less precise than the logged-in distinguishability endpoint but works from
    any server including Cloud Run."""
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True,
                          headers={"User-Agent": _SCC_UA}) as client:
            resp = client.get(SCC_ENTITY_SEARCH_URL)
            resp.raise_for_status()
            html = resp.text

            post_data = _extract_inputs(html)

            # Field for the business name: id ends with "txtBusinessName"
            name_field = _find_name_by_id_suffix(html, "txtBusinessName")
            if not name_field:
                name_field = next(
                    (k for k in post_data
                     if "businessname" in k.lower() or "entityname" in k.lower()),
                    None,
                )
            if not name_field:
                return {
                    "available": None,
                    "status": "UNAVAILABLE",
                    "message": "SCC name check temporarily unavailable. Our team will verify before filing.",
                    "link": SCC_ENTITY_SEARCH_URL,
                    "conflicts": [],
                    "raw": "",
                }

            post_data[name_field] = business_name

            # Set search logic to Exact Match (value '3')
            logic_field = _find_name_by_id_suffix(html, "ddlSearchLogic")
            if logic_field:
                post_data[logic_field] = "3"
            else:
                for k in list(post_data):
                    if "searchlogic" in k.lower():
                        post_data[k] = "3"
                        break

            resp2 = client.post(SCC_ENTITY_SEARCH_URL, data=post_data)
            resp2.raise_for_status()
            body = resp2.text
            body_lower = body.lower()

            if (
                "no records found" in body_lower
                or "0 records" in body_lower
                or "no results" in body_lower
            ):
                return {
                    "available": True,
                    "status": "AVAILABLE",
                    "message": f'"{business_name}" appears to be available on Virginia SCC.',
                    "conflicts": [],
                    "raw": "",
                }

            conflicts = _parse_entity_names(body)

            if business_name.upper() in body.upper():
                return {
                    "available": False,
                    "status": "TAKEN",
                    "message": f'"{business_name}" already exists on Virginia SCC.',
                    "conflicts": conflicts,
                    "raw": "",
                }

            # Results found but no exact match — distinguishability unclear
            return {
                "available": None,
                "status": "UNKNOWN",
                "message": (
                    "Similar names exist on Virginia SCC. "
                    "Our team will confirm your name's availability before filing."
                ),
                "link": SCC_ENTITY_SEARCH_URL,
                "conflicts": conflicts,
                "raw": "",
            }

    except httpx.TimeoutException:
        return {
            "available": None,
            "status": "UNAVAILABLE",
            "message": "Virginia SCC name check timed out. Our team will verify before filing.",
            "link": SCC_ENTITY_SEARCH_URL,
            "conflicts": [],
            "raw": "",
        }
    except Exception as e:
        print(f"⚠️ SCC public name check failed: {e}")
        return {
            "available": None,
            "status": "UNAVAILABLE",
            "message": "Virginia SCC name check temporarily unavailable. Our team will verify before filing.",
            "link": SCC_ENTITY_SEARCH_URL,
            "conflicts": [],
            "raw": "",
        }

# ── Chrome CDP fallback ─────────────────────────────────────────────────────

def _connect_to_local_chrome(p):
    """Connects to a running local Chrome via CDP — only works from a dev
    machine with Chrome open, never from a deployed server like Cloud Run."""
    return p.chromium.connect_over_cdp("http://172.27.176.1:9222")

def _check_name_chrome(business_name: str) -> dict:
    """Logged-in SCC distinguishability check via local Chrome CDP."""
    with sync_playwright() as p:
        try:
            browser = _connect_to_local_chrome(p)
        except Exception:
            return {
                "available": None,
                "status": "UNAVAILABLE",
                "message": "SCC name check is temporarily unavailable. Our team will verify before filing.",
                "link": SCC_NAME_CHECK_URL,
                "conflicts": [],
                "raw": "",
            }
        context = browser.contexts[0]
        page = context.new_page()

        page.goto("https://cis.scc.virginia.gov/Account/Login")
        page.wait_for_load_state("networkidle")
        page.fill('#txtUsername', SCC_USERNAME)
        page.fill('#txtPassword', SCC_PASSWORD)
        page.click('#Login')
        page.wait_for_load_state("networkidle")

        page.goto(SCC_NAME_CHECK_URL)
        page.wait_for_load_state("networkidle")

        page.fill('#txtEntityName', business_name)
        page.click('#Check_Name')
        page.wait_for_function("!document.body.innerText.includes('Please wait')", timeout=15000)
        page.wait_for_timeout(2000)

        body_text = page.inner_text("body")
        page.close()

        if "is not distinguishable" in body_text.lower():
            conflicts = []
            for line in body_text.split('\n'):
                if 'Active' in line or 'Inactive' in line:
                    parts = line.strip().split('\t')
                    if len(parts) >= 2:
                        conflicts.append(parts[1].strip())
            conflict_str = ", ".join(conflicts) if conflicts else "existing Virginia businesses"
            return {
                "available": False,
                "status": "TAKEN",
                "message": f'"{business_name}" is not distinguishable from {conflict_str}.',
                "conflicts": conflicts,
                "raw": body_text,
            }

        if "is distinguishable" in body_text.lower():
            return {
                "available": True,
                "status": "AVAILABLE",
                "message": f'"{business_name}" is available on the Virginia SCC database!',
                "conflicts": [],
                "raw": body_text,
            }

        return {
            "available": None,
            "status": "UNKNOWN",
            "message": "Could not determine availability from Virginia SCC.",
            "link": SCC_NAME_CHECK_URL,
            "conflicts": [],
            "raw": body_text,
        }

# ── Public entrypoint ───────────────────────────────────────────────────────

def check_name_on_scc(business_name: str) -> dict:
    """Check Virginia SCC name availability.
    Order: (1) public HTTP search — works anywhere; (2) logged-in Chrome CDP
    — works only on dev machine; (3) graceful fallback with manual-verify flag."""
    # Try public HTTP search first (no Chrome required)
    try:
        public_result = check_name_public(business_name)
    except Exception as e:
        print(f"⚠️ check_name_public raised: {e}")
        public_result = {
            "available": None,
            "status": "UNAVAILABLE",
            "message": "SCC name check temporarily unavailable. Our team will verify before filing.",
            "link": SCC_ENTITY_SEARCH_URL,
            "conflicts": [],
            "raw": "",
        }

    # Definitive answer — no need to fall back
    if public_result.get("available") is not None:
        return public_result

    # Inconclusive — try Chrome for a proper distinguishability check
    try:
        chrome_result = _check_name_chrome(business_name)
        if chrome_result.get("available") is not None or chrome_result.get("status") != "UNAVAILABLE":
            return chrome_result
    except Exception as e:
        print(f"⚠️ _check_name_chrome raised: {e}")

    # Both failed — return graceful fallback so the order can proceed
    return {
        "available": None,
        "status": "UNAVAILABLE",
        "message": (
            "Name availability check temporarily unavailable. "
            "Our team will manually verify your name before filing your LLC."
        ),
        "link": SCC_ENTITY_SEARCH_URL,
        "needs_manual_name_check": True,
        "conflicts": [],
        "raw": "",
    }


def check_llc_exists_on_scc(business_name: str) -> dict:
    """Verifies an LLC a customer claims to already have is actually on the
    Virginia SCC's public Business Entity Search. Tries public HTTP first,
    then Chrome CDP."""
    # Try public search first
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True,
                          headers={"User-Agent": _SCC_UA}) as client:
            resp = client.get(SCC_ENTITY_SEARCH_URL)
            resp.raise_for_status()
            html = resp.text

            post_data = _extract_inputs(html)
            name_field = _find_name_by_id_suffix(html, "txtBusinessName")
            if not name_field:
                name_field = next(
                    (k for k in post_data if "businessname" in k.lower() or "entityname" in k.lower()),
                    None,
                )

            if name_field:
                post_data[name_field] = business_name
                logic_field = _find_name_by_id_suffix(html, "ddlSearchLogic")
                if logic_field:
                    post_data[logic_field] = "3"  # Exact Match

                resp2 = client.post(SCC_ENTITY_SEARCH_URL, data=post_data)
                resp2.raise_for_status()
                body = resp2.text
                body_lower = body.lower()

                if "no records found" in body_lower or "0 records" in body_lower:
                    return {
                        "exists": False,
                        "message": (
                            "We could not find this LLC in the Virginia SCC database. "
                            "Please double check the name or uncheck this box to form a new LLC."
                        ),
                    }

                if business_name.lower() in body.lower():
                    return {
                        "exists": True,
                        "message": f"{business_name} found in Virginia SCC records",
                    }
    except Exception as e:
        print(f"⚠️ check_llc_exists_on_scc public HTTP failed: {e}")

    # Fall back to Chrome CDP
    try:
        with sync_playwright() as p:
            try:
                browser = _connect_to_local_chrome(p)
            except Exception:
                return {
                    "exists": None,
                    "status": "UNAVAILABLE",
                    "message": "We can't verify existing LLCs against Virginia SCC right now.",
                    "link": SCC_ENTITY_SEARCH_URL,
                }
            context = browser.contexts[0]
            page = context.new_page()

            page.goto("https://cis.scc.virginia.gov/Account/Login")
            page.wait_for_load_state("networkidle")
            if page.locator('#txtUsername').count() > 0:
                page.fill('#txtUsername', SCC_USERNAME)
                page.fill('#txtPassword', SCC_PASSWORD)
                page.click('#Login')
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(2000)

            page.goto(SCC_ENTITY_SEARCH_URL)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
            page.select_option('#BEFilingSearch_ddlSearchLogic', value='3')
            page.fill('#BusinessSearch_Index_txtBusinessName', business_name)
            page.wait_for_timeout(300)
            page.click('#btnSearch')
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)

            body = page.inner_text("body")
            page.close()

            if "no records found" in body.lower():
                return {
                    "exists": False,
                    "message": (
                        "We could not find this LLC in the Virginia SCC database. "
                        "Please double check the name or uncheck this box to form a new LLC."
                    ),
                }

            if business_name.lower() in body.lower():
                return {
                    "exists": True,
                    "message": f"{business_name} found in Virginia SCC records",
                }

            return {
                "exists": None,
                "message": "Could not determine whether this LLC exists. Please check the Virginia SCC website directly.",
            }
    except Exception as e:
        return {
            "exists": None,
            "message": f"Could not connect to Virginia SCC: {str(e)}",
        }
