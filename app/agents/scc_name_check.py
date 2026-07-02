import re
import time

import httpx
from bs4 import BeautifulSoup
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

# ── Name sanitization and validation ───────────────────────────────────────
# Virginia SCC allowed characters: letters, numbers, spaces, hyphens,
# apostrophes, periods, commas, ampersands, exclamation marks, plus signs.

_DISALLOWED = re.compile(r"[^A-Za-z0-9 \-'.,&!+]")
_LLC_SUFFIX = re.compile(r"\bL\.?L\.?C\.?\s*$", re.IGNORECASE)
_MULTI_SPACE = re.compile(r" {2,}")


def sanitize_business_name(name: str) -> tuple[str, str | None]:
    """Clean and validate a business name before sending to Virginia SCC.

    Returns (cleaned_name, error_message).
    error_message is None when the name is valid after cleaning.
    """
    name = name.strip()

    # Strip characters Virginia SCC does not allow
    cleaned = _DISALLOWED.sub("", name)

    # Collapse runs of spaces created by removal
    cleaned = _MULTI_SPACE.sub(" ", cleaned).strip()

    if not cleaned:
        return cleaned, "Please enter a business name"

    if not _LLC_SUFFIX.search(cleaned):
        return cleaned, 'Business name must end with "LLC"'

    # After removing the LLC suffix, the remaining text must not be empty
    base = _LLC_SUFFIX.sub("", cleaned).strip()
    if not base:
        return cleaned, "Please enter your full business name, not just LLC"

    if len(cleaned) < 5:
        return cleaned, "Business name is too short — please enter your full name"

    return cleaned, None


# ── Name-check result cache ─────────────────────────────────────────────────
# Keyed by normalized (uppercased) business name. Only definitive results
# (AVAILABLE / TAKEN) are cached — UNKNOWN and UNAVAILABLE are always
# re-checked so we don't permanently mask an edge case.

_name_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 3600  # 1 hour


def _cache_get(name: str) -> dict | None:
    key = name.strip().upper()
    entry = _name_cache.get(key)
    if not entry:
        return None
    result, ts = entry
    if time.monotonic() - ts < _CACHE_TTL:
        print(f"[name-check] Cache hit for '{name}'")
        return result
    del _name_cache[key]
    return None


def _cache_set(name: str, result: dict) -> None:
    _name_cache[name.strip().upper()] = (result, time.monotonic())

# ── HTML parsing helpers ────────────────────────────────────────────────────

def _parse_scc_form(html: str) -> tuple[dict, str | None, str | None]:
    """Parse an SCC search page into (post_data, name_field, logic_field).

    post_data: {name: value} for every input/select except radio/checkbox/button,
               including the ASP.NET __RequestVerificationToken.
    name_field: the name= attribute of the business-name text input.
    logic_field: the name= attribute of the search-logic dropdown.
    """
    soup = BeautifulSoup(html, "html.parser")

    post_data: dict[str, str] = {}
    skip_types = {"radio", "checkbox", "submit", "button", "image", "reset"}
    for inp in soup.find_all("input"):
        if inp.get("type", "").lower() in skip_types:
            continue
        name = inp.get("name")
        if name:
            post_data[name] = inp.get("value", "")

    # Log whether the anti-forgery token is present
    token = soup.find("input", {"name": "__RequestVerificationToken"})
    print(f"[name-check] __RequestVerificationToken found: {token is not None}")

    # Business-name field: prefer id ending with txtBusinessName, then fallback
    name_input = soup.find("input", id=lambda x: x and x.endswith("txtBusinessName"))
    if not name_input:
        name_input = soup.find(
            "input",
            attrs={"name": lambda x: x and (
                "businessname" in x.lower() or "entityname" in x.lower()
            )},
        )
    name_field = name_input.get("name") if name_input else None

    # Search-logic dropdown: prefer id ending with ddlSearchLogic, then fallback
    logic_select = soup.find("select", id=lambda x: x and x.endswith("ddlSearchLogic"))
    if not logic_select:
        logic_select = soup.find(
            "select",
            attrs={"name": lambda x: x and "searchlogic" in x.lower()},
        )
    if logic_select:
        post_data[logic_select.get("name", "")] = post_data.get(logic_select.get("name", ""), "")
    logic_field = logic_select.get("name") if logic_select else None

    return post_data, name_field, logic_field


def _parse_scc_results(html: str, business_name: str) -> tuple[bool | None, list[str]]:
    """Parse SCC results page into (available, conflicts).

    available=True  → no matching records found
    available=False → exact match for business_name found
    available=None  → similar names exist but no exact match (UNKNOWN)
    conflicts: up to 5 entity names pulled from result table cells
    """
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    if re.search(r"no records found|0 records|no results", page_text, re.IGNORECASE):
        return True, []

    # Pull entity names from table cells — any cell that looks like a business name
    conflicts: list[str] = []
    for td in soup.find_all("td"):
        cell = td.get_text(strip=True)
        if (
            4 < len(cell) < 100
            and re.search(r"\b(LLC|INC|CORP|LTD|LP|LLP|PLLC|PC)\b", cell, re.IGNORECASE)
        ):
            if cell not in conflicts:
                conflicts.append(cell)
    conflicts = conflicts[:5]

    if business_name.upper() in html.upper():
        return False, conflicts

    return None, conflicts

# ── Public HTTP search (no Chrome / no login) ───────────────────────────────

_UNAVAILABLE = {
    "available": None,
    "status": "UNAVAILABLE",
    "message": "⚠️ Name check temporarily unavailable - you can still continue. Our team verifies all names before filing.",
    "link": SCC_ENTITY_SEARCH_URL,
    "conflicts": [],
    "raw": "",
}


def check_name_public(business_name: str) -> dict:
    """Search the SCC public entity database via plain HTTP — no Chrome or login.
    Less precise than the logged-in distinguishability endpoint but works from
    any server including Cloud Run.

    Retries up to 3 times with 2-second delays (5 s per-attempt timeout).
    Successful AVAILABLE/TAKEN results are cached for 1 hour.
    """
    cached = _cache_get(business_name)
    if cached:
        return cached

    last_error = "unknown"

    for attempt in range(1, 4):
        try:
            with httpx.Client(timeout=5.0, follow_redirects=True,
                              headers={"User-Agent": _SCC_UA}) as client:
                resp = client.get(SCC_ENTITY_SEARCH_URL)
                print(f"[name-check] GET {SCC_ENTITY_SEARCH_URL} → {resp.status_code} "
                      f"({len(resp.text)} bytes) attempt {attempt}/3")
                resp.raise_for_status()

                post_data, name_field, logic_field = _parse_scc_form(resp.text)
                print(f"[name-check] name_field={name_field!r} logic_field={logic_field!r} "
                      f"post_data keys={list(post_data.keys())}")

                if not name_field:
                    last_error = f"name field not found in SCC form (attempt {attempt}/3)"
                    print(f"[name-check] {last_error} for '{business_name}'")
                    if attempt < 3:
                        time.sleep(2)
                    continue

                post_data[name_field] = business_name
                if logic_field:
                    post_data[logic_field] = "3"  # Exact Match

                resp2 = client.post(SCC_ENTITY_SEARCH_URL, data=post_data)
                print(f"[name-check] POST → {resp2.status_code} ({len(resp2.text)} bytes)")
                resp2.raise_for_status()

                available, conflicts = _parse_scc_results(resp2.text, business_name)
                print(f"[name-check] result for '{business_name}': available={available} "
                      f"conflicts={conflicts}")

                if available is True:
                    result = {
                        "available": True,
                        "status": "AVAILABLE",
                        "message": f'"{business_name}" appears to be available on Virginia SCC.',
                        "conflicts": [],
                        "raw": "",
                    }
                    _cache_set(business_name, result)
                    return result

                if available is False:
                    result = {
                        "available": False,
                        "status": "TAKEN",
                        "message": f'"{business_name}" already exists on Virginia SCC.',
                        "conflicts": conflicts,
                        "raw": "",
                    }
                    _cache_set(business_name, result)
                    return result

                # available is None — similar names found but no exact match
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

        except httpx.TimeoutException as e:
            last_error = f"timeout on attempt {attempt}/3"
            print(f"[name-check] {last_error} for '{business_name}': {e}")
        except httpx.HTTPStatusError as e:
            last_error = f"HTTP {e.response.status_code} on attempt {attempt}/3"
            print(f"[name-check] {last_error} for '{business_name}': {e}")
        except Exception as e:
            last_error = f"{type(e).__name__} on attempt {attempt}/3: {e}"
            print(f"[name-check] {last_error} for '{business_name}'")

        if attempt < 3:
            time.sleep(2)

    print(f"[name-check] All 3 attempts failed for '{business_name}'. Last failure: {last_error}")
    return _UNAVAILABLE

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
        print(f"[name-check] check_name_public raised unexpectedly: {type(e).__name__}: {e}")
        public_result = _UNAVAILABLE

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
    return {**_UNAVAILABLE, "needs_manual_name_check": True}


def check_llc_exists_on_scc(business_name: str) -> dict:
    """Verifies an LLC a customer claims to already have is actually on the
    Virginia SCC's public Business Entity Search. Tries public HTTP first,
    then Chrome CDP."""
    # Try public search first
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True,
                          headers={"User-Agent": _SCC_UA}) as client:
            resp = client.get(SCC_ENTITY_SEARCH_URL)
            print(f"[llc-exists] GET {SCC_ENTITY_SEARCH_URL} → {resp.status_code} ({len(resp.text)} bytes)")
            resp.raise_for_status()

            post_data, name_field, logic_field = _parse_scc_form(resp.text)

            if name_field:
                post_data[name_field] = business_name
                if logic_field:
                    post_data[logic_field] = "3"  # Exact Match

                resp2 = client.post(SCC_ENTITY_SEARCH_URL, data=post_data)
                print(f"[llc-exists] POST → {resp2.status_code} ({len(resp2.text)} bytes)")
                resp2.raise_for_status()

                available, _ = _parse_scc_results(resp2.text, business_name)

                if available is True:
                    return {
                        "exists": False,
                        "message": (
                            "We could not find this LLC in the Virginia SCC database. "
                            "Please double check the name or uncheck this box to form a new LLC."
                        ),
                    }

                if available is False:
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
