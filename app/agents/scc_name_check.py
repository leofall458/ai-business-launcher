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
SCC_COOKIE_CONSENT_URL = "https://cis.scc.virginia.gov/Cookie/StoreCookieConsent"

# Unauthenticated JSON/AJAX endpoints behind SCC_NAME_CHECK_URL's own "Name
# Check" button (see Checkeavailability()/GetEntitySearchResults() in that
# page's <script>) - unlike SCC_ENTITY_SEARCH_URL, these are not gated by
# reCAPTCHA v3. Confirmed by hand: real filed LLCs (prod customers "Paws
# LLC", "Govcon Ramp LLC") correctly come back not-distinguishable, a
# nonsense name and a staging-only order that was never actually filed
# ("Nova Cloud Consulting LLC") correctly come back distinguishable.
SCC_CHECK_DISTINGUISHABLE_URL = "https://cis.scc.virginia.gov/DocumentProcessingHelper/CheckEntityDistinguishableCheckForOnline"
SCC_EXACT_MATCH_URL = "https://cis.scc.virginia.gov/DocumentProcessingHelper/EntitySearchExactMatchNameAvail"

_SCC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# The old 10s per-attempt timeout was tighter than the live site's own
# typical response time - the CheckEntityDistinguishableCheckForOnline POST
# has been directly observed taking 10-12+ seconds on a normal, working
# response (see run_scc_health_check's per-attempt latency logging), so a
# genuinely fine SCC response was routinely getting cut off mid-request and
# forced into a pointless retry. 20s gives real headroom above that.
#
# Retries dropped from 3 to 2 at the same time: with the old too-tight
# timeout, 3 retries were doing double duty as a workaround for spurious
# timeouts on an otherwise-working site. Now that the timeout properly
# reflects real-world latency, that workaround isn't needed - and keeping
# 3 attempts at 20s would let a genuinely down/hanging SCC block a customer
# for up to ~64s (3*20s + 2*2s) before falling back, well past what feels
# like a reasonable wait with a loading indicator. 2 attempts at 20s caps
# the true-failure case at ~42s while still giving one clean retry for a
# real transient blip.
SCC_PUBLIC_CHECK_TIMEOUT_SECONDS = 20.0
SCC_PUBLIC_CHECK_ATTEMPTS = 2
SCC_PUBLIC_CHECK_RETRY_DELAY = 2

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


def _get_name_check_token(client: httpx.Client) -> str:
    """Accepts the cookie-consent gate then GETs SCC_NAME_CHECK_URL for a
    fresh anti-forgery token - every DocumentProcessingHelper POST below
    needs one in an __RequestVerificationToken header (matching what the
    page's own JS does via beforeSend/headers), even though testing found
    the endpoint doesn't actually reject a request missing it - sending it
    anyway matches real browser behavior and costs nothing."""
    client.post(SCC_COOKIE_CONSENT_URL, headers={"X-Requested-With": "XMLHttpRequest"})
    resp = client.get(SCC_NAME_CHECK_URL)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    token_input = soup.find("input", {"name": "__RequestVerificationToken"})
    if not token_input or not token_input.get("value"):
        raise RuntimeError("no __RequestVerificationToken on SCC_NAME_CHECK_URL")
    return token_input["value"]


def _get_exact_match_conflicts(client: httpx.Client, token: str, business_name: str) -> list[str]:
    """Follow-up call the page's own JS also makes after a distinguishability
    check (GetEntitySearchResults) - returns an HTML table of the matching
    entities, purely for display; the availability verdict itself already
    came from _get_name_check_token's sibling call and doesn't depend on
    this succeeding, so any failure here is non-fatal (returns [])."""
    try:
        resp = client.post(SCC_EXACT_MATCH_URL, data={
            "searchIdValue": "", "searchNameValue": business_name,
        }, headers={
            "__RequestVerificationToken": token,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": SCC_NAME_CHECK_URL,
        })
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        conflicts = []
        for row in soup.select("#grid_businessList tbody tr"):
            cells = row.find_all("td")
            if len(cells) >= 2:
                name = cells[1].get_text(strip=True)
                if name and name not in conflicts:
                    conflicts.append(name)
        return conflicts[:5]
    except Exception as e:
        print(f"[name-check] Could not fetch conflict list for '{business_name}': {e}")
        return []


def check_name_public(business_name: str) -> dict:
    """Checks Virginia SCC name distinguishability via the same
    unauthenticated JSON endpoint SCC_NAME_CHECK_URL's own "Name Check"
    button calls (CheckEntityDistinguishableCheckForOnline) - unlike
    SCC_ENTITY_SEARCH_URL (see the old _parse_scc_form/_parse_scc_results
    below, kept only in case this ever needs a fallback), this one is not
    gated by reCAPTCHA v3 and needs no login. Works from any server
    including Cloud Run.

    Retries up to SCC_PUBLIC_CHECK_ATTEMPTS times with
    SCC_PUBLIC_CHECK_RETRY_DELAY-second delays
    (SCC_PUBLIC_CHECK_TIMEOUT_SECONDS-second per-attempt timeout).
    Successful AVAILABLE/TAKEN results are cached for 1 hour.
    """
    cached = _cache_get(business_name)
    if cached:
        return cached

    last_error = "unknown"

    for attempt in range(1, SCC_PUBLIC_CHECK_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=SCC_PUBLIC_CHECK_TIMEOUT_SECONDS, follow_redirects=True,
                              headers={"User-Agent": _SCC_UA}) as client:
                token = _get_name_check_token(client)

                resp = client.post(SCC_CHECK_DISTINGUISHABLE_URL, data={
                    "searchNameValue": business_name, "businessTypeName": "",
                    "Filingtype": "", "IsOnline": "true", "IsExternalCheckAvailability": "true",
                }, headers={
                    "__RequestVerificationToken": token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": SCC_NAME_CHECK_URL,
                })
                print(f"[name-check] POST {SCC_CHECK_DISTINGUISHABLE_URL} → {resp.status_code} "
                      f"attempt {attempt}/{SCC_PUBLIC_CHECK_ATTEMPTS}")
                resp.raise_for_status()
                data = resp.json()
                result_data = data.get("Result") or {}
                is_distinguished = result_data.get("IsDistinguished")
                message = data.get("Message") or ""
                print(f"[name-check] result for '{business_name}': IsDistinguished={is_distinguished} "
                      f"Message={message!r}")

                if is_distinguished is True:
                    result = {
                        "available": True,
                        "status": "AVAILABLE",
                        "message": f'"{business_name}" is available on Virginia SCC.',
                        "conflicts": [],
                        "raw": "",
                    }
                    _cache_set(business_name, result)
                    return result

                if is_distinguished is False:
                    conflicts = _get_exact_match_conflicts(client, token, business_name)
                    result = {
                        "available": False,
                        "status": "TAKEN",
                        "message": message or f'"{business_name}" already exists on Virginia SCC.',
                        "conflicts": conflicts,
                        "raw": "",
                    }
                    _cache_set(business_name, result)
                    return result

                # IsDistinguished missing/None - unexpected response shape,
                # worth a retry rather than trusting a result we don't understand.
                last_error = f"unexpected response shape (no IsDistinguished): {data}"
                print(f"[name-check] {last_error} for '{business_name}'")

        except httpx.TimeoutException as e:
            last_error = f"timeout on attempt {attempt}/{SCC_PUBLIC_CHECK_ATTEMPTS}"
            print(f"[name-check] {last_error} for '{business_name}': {e}")
        except httpx.HTTPStatusError as e:
            last_error = f"HTTP {e.response.status_code} on attempt {attempt}/{SCC_PUBLIC_CHECK_ATTEMPTS}"
            print(f"[name-check] {last_error} for '{business_name}': {e}")
        except Exception as e:
            last_error = f"{type(e).__name__} on attempt {attempt}/{SCC_PUBLIC_CHECK_ATTEMPTS}: {e}"
            print(f"[name-check] {last_error} for '{business_name}'")

        if attempt < SCC_PUBLIC_CHECK_ATTEMPTS:
            time.sleep(SCC_PUBLIC_CHECK_RETRY_DELAY)

    print(f"[name-check] All {SCC_PUBLIC_CHECK_ATTEMPTS} attempts failed for '{business_name}'. Last failure: {last_error}")
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
    # Try public search first - same unauthenticated, non-reCAPTCHA-gated
    # endpoint as check_name_public (see its docstring); "distinguishable"
    # here just means "not found", i.e. exists=False. Same timeout constant
    # as check_name_public - it's the identical SCC_CHECK_DISTINGUISHABLE_URL
    # endpoint with the same 10-12+ second real-world latency, no retry loop
    # here so no need for a separate attempts constant, just the timeout.
    try:
        with httpx.Client(timeout=SCC_PUBLIC_CHECK_TIMEOUT_SECONDS, follow_redirects=True,
                          headers={"User-Agent": _SCC_UA}) as client:
            token = _get_name_check_token(client)

            resp = client.post(SCC_CHECK_DISTINGUISHABLE_URL, data={
                "searchNameValue": business_name, "businessTypeName": "",
                "Filingtype": "", "IsOnline": "true", "IsExternalCheckAvailability": "true",
            }, headers={
                "__RequestVerificationToken": token,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": SCC_NAME_CHECK_URL,
            })
            print(f"[llc-exists] POST {SCC_CHECK_DISTINGUISHABLE_URL} → {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            is_distinguished = (data.get("Result") or {}).get("IsDistinguished")
            print(f"[llc-exists] result for '{business_name}': IsDistinguished={is_distinguished}")

            if is_distinguished is True:
                return {
                    "exists": False,
                    "message": (
                        "We could not find this LLC in the Virginia SCC database. "
                        "Please double check the name or uncheck this box to form a new LLC."
                    ),
                }

            if is_distinguished is False:
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
        print(f"⚠️ check_llc_exists_on_scc crashed for '{business_name}': {e}")
        return {
            "exists": None,
            "message": "Could not connect to Virginia SCC right now. Please check the Virginia SCC website directly.",
        }

# ── Health check (monitoring only) ──────────────────────────────────────────
# Deliberately independent of check_name_public/check_name_on_scc above -
# does not touch the cache, the 3x retry loop, or the Chrome CDP fallback,
# and does not change anything about how a real customer's name check
# behaves. Its only job is to walk the same NameCheckAvailability ->
# CheckEntityDistinguishableCheckForOnline chain step by step and capture
# exactly what broke (and the raw response) if SCC ever changes their page.

SCC_HEALTH_CHECK_TEST_NAME = "Apex Federal Advisors LLC"


SCC_HEALTH_CHECK_ATTEMPTS = 3  # matches check_name_public's own retry count/delay -
SCC_HEALTH_CHECK_RETRY_DELAY = 2  # the live site is slow enough (~9-10s POSTs
                                   # observed) that a single timeout is
                                   # transient noise, not proof of an outage.


def run_scc_health_check(business_name: str = SCC_HEALTH_CHECK_TEST_NAME) -> dict:
    """Diagnostic probe of the SCC name-check dependency chain. Never
    raises - always returns a dict with at least "ok" and "steps".

    Retries the whole probe up to SCC_HEALTH_CHECK_ATTEMPTS times, same
    count/delay as check_name_public's own retry loop - the live SCC POST
    has been observed taking ~9-10s on its own, right at the edge of the
    10s per-attempt timeout, so a single slow response isn't reliable
    evidence the chain is actually broken.

    "steps" records what happened at each stage, across every attempt, so
    a failure alert always has something concrete to debug from, not just
    "it failed somewhere". On failure, "raw" holds the actual response
    body/error text (truncated) from the last attempt that didn't match
    what we expected - this is the part that lets us tell "SCC is slow/
    down" apart from "SCC changed their page and our scraper needs
    updating"."""
    steps: list[dict] = []

    def record(step: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": step, "ok": ok, "detail": detail})

    def failure(failed_step: str, raw: str) -> dict:
        raw = (raw or "")[:2000]
        print(f"[scc-health-check] FAILED at step '{failed_step}' for '{business_name}' "
              f"after {SCC_HEALTH_CHECK_ATTEMPTS} attempts. "
              f"Raw response/error (truncated to 2000 chars): {raw}")
        return {"ok": False, "failed_step": failed_step, "steps": steps, "raw": raw}

    last_failed_step = "unexpected"
    last_raw = ""

    for attempt in range(1, SCC_HEALTH_CHECK_ATTEMPTS + 1):
        try:
            # check_name_public uses a 10s timeout because a real customer
            # is waiting on that request; this health check has no such
            # constraint (background, once-a-day), and the live POST has
            # been observed taking ~9-10s on its own - a 10s timeout here
            # would mostly measure "is SCC on the slow side of normal
            # today" rather than "is the chain actually broken".
            with httpx.Client(timeout=20.0, follow_redirects=True,
                              headers={"User-Agent": _SCC_UA}) as client:
                try:
                    consent_resp = client.post(SCC_COOKIE_CONSENT_URL, headers={"X-Requested-With": "XMLHttpRequest"})
                    record("cookie_consent", True, f"attempt {attempt}: HTTP {consent_resp.status_code}")
                except Exception as e:
                    # Best-effort like the real check-flow treats it - not
                    # fatal on its own, but worth recording if it ever
                    # starts failing.
                    record("cookie_consent", False, f"attempt {attempt}: {type(e).__name__}: {e}")

                try:
                    token_resp = client.get(SCC_NAME_CHECK_URL)
                    token_resp.raise_for_status()
                except Exception as e:
                    record("fetch_token_page", False, f"attempt {attempt}: {type(e).__name__}: {e}")
                    last_failed_step, last_raw = "fetch_token_page", str(e)
                    continue

                soup = BeautifulSoup(token_resp.text, "html.parser")
                token_input = soup.find("input", {"name": "__RequestVerificationToken"})
                if not token_input or not token_input.get("value"):
                    record("fetch_token_page", False, f"attempt {attempt}: no __RequestVerificationToken input found on page")
                    last_failed_step, last_raw = "fetch_token_page", token_resp.text
                    continue
                token = token_input["value"]
                record("fetch_token_page", True, f"attempt {attempt}: HTTP {token_resp.status_code}, token length {len(token)}")

                post_started = time.monotonic()
                try:
                    check_resp = client.post(SCC_CHECK_DISTINGUISHABLE_URL, data={
                        "searchNameValue": business_name, "businessTypeName": "",
                        "Filingtype": "", "IsOnline": "true", "IsExternalCheckAvailability": "true",
                    }, headers={
                        "__RequestVerificationToken": token,
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": SCC_NAME_CHECK_URL,
                    })
                except Exception as e:
                    elapsed = time.monotonic() - post_started
                    record("check_distinguishable", False, f"attempt {attempt} ({elapsed:.1f}s): {type(e).__name__}: {e}")
                    last_failed_step, last_raw = "check_distinguishable", str(e)
                    continue
                post_elapsed = time.monotonic() - post_started

                if check_resp.status_code != 200:
                    record("check_distinguishable", False, f"attempt {attempt} ({post_elapsed:.1f}s): HTTP {check_resp.status_code}")
                    last_failed_step, last_raw = "check_distinguishable", check_resp.text
                    continue

                content_type = check_resp.headers.get("content-type", "")
                if "json" not in content_type.lower():
                    record("check_distinguishable", False, f"attempt {attempt} ({post_elapsed:.1f}s): non-JSON content-type: {content_type!r}")
                    last_failed_step, last_raw = "check_distinguishable", check_resp.text
                    continue

                try:
                    data = check_resp.json()
                except Exception as e:
                    record("check_distinguishable", False, f"attempt {attempt} ({post_elapsed:.1f}s): could not parse JSON body: {e}")
                    last_failed_step, last_raw = "check_distinguishable", check_resp.text
                    continue

                result_data = data.get("Result")
                is_distinguished = result_data.get("IsDistinguished") if isinstance(result_data, dict) else None
                if not isinstance(is_distinguished, bool):
                    record("check_distinguishable", False, f"attempt {attempt} ({post_elapsed:.1f}s): unexpected JSON shape (no boolean IsDistinguished): {data!r}")
                    last_failed_step, last_raw = "check_distinguishable", repr(data)
                    continue

                record("check_distinguishable", True, f"attempt {attempt} ({post_elapsed:.1f}s): IsDistinguished={is_distinguished}")

                # SCC_HEALTH_CHECK_TEST_NAME is a made-up name we don't
                # expect to ever actually be registered, so
                # IsDistinguished=True is the expected outcome - but a
                # False here isn't proof the chain is broken (someone
                # could have genuinely filed this exact name), so it's
                # only reported as a mismatch, never an alert trigger.
                matched_expectation = is_distinguished is True

                print(f"[scc-health-check] OK - '{business_name}' IsDistinguished={is_distinguished} "
                      f"(expected True, matched={matched_expectation}) on attempt {attempt}/{SCC_HEALTH_CHECK_ATTEMPTS}")
                return {
                    "ok": True,
                    "business_name": business_name,
                    "is_distinguished": is_distinguished,
                    "matched_expectation": matched_expectation,
                    "steps": steps,
                }
        except Exception as e:
            record("unexpected", False, f"attempt {attempt}: {type(e).__name__}: {e}")
            last_failed_step, last_raw = "unexpected", str(e)

        if attempt < SCC_HEALTH_CHECK_ATTEMPTS:
            time.sleep(SCC_HEALTH_CHECK_RETRY_DELAY)

    return failure(last_failed_step, last_raw)
