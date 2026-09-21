"""Automatic Virginia SCC name-availability check for a person who just wants
to know "is my name available?" - the pre-payment widget on the marketing
site (POST /api/name-check) and the inline check on the post-payment name
step both go through here.

Uses check_name_public directly (the unauthenticated CheckEntityDistinguishable
JSON endpoint, which works from Cloud Run and is what the daily SCC health
check monitors) rather than check_name_on_scc: that wrapper's fallback drives a
local Chrome over CDP, which on Cloud Run can only sit and time out.

When a name is TAKEN, it also checks a handful of close variants in parallel
and returns the ones SCC says are available right now - a name someone can
actually use is a much better next step than a dead end.
"""

import concurrent.futures as cf
import re

from app.agents.scc_name_check import check_name_public, sanitize_business_name

_LLC_TAIL = re.compile(r"[\s,]*\bL\.?L\.?C\.?\s*$", re.IGNORECASE)

# (prefix, suffix) pairs applied around the name's base (the name minus "LLC").
# Regional/neutral words SCC has been observed to treat as distinguishing
# (e.g. "Apple LLC" is taken but "Blue Ridge Apple LLC" is available).
_VARIANTS = (
    ("", "Group"),
    ("Blue Ridge", ""),
    ("", "Partners"),
    ("Commonwealth", ""),
    ("", "Co"),
    ("", "Solutions"),
)

ALTERNATIVES_TO_CHECK = 5
MAX_ALTERNATIVES = 3
# The SCC endpoint is slow (10s+ on a bad day) - never make a visitor wait
# longer than this for the bonus alternatives; the main answer is already in.
ALTERNATIVES_DEADLINE_SECONDS = 15.0
MAX_NAME_LENGTH = 100


def normalize_input_name(raw: str) -> str:
    """Visitors often type just "Apex Federal Advisors" - Virginia requires the
    LLC designation, so add it rather than bouncing them with an error."""
    name = (raw or "").strip()
    if name and not _LLC_TAIL.search(name):
        name = f"{name} LLC"
    return name


def suggest_variants(cleaned_name: str) -> list:
    """Close variants of an "... LLC" name, skipping any whose added word is
    already in the name (no "Acme Group Group LLC")."""
    base = _LLC_TAIL.sub("", cleaned_name).strip(" ,")
    if not base:
        return []
    lowered = base.lower()
    out, seen = [], {cleaned_name.lower()}
    for prefix, suffix in _VARIANTS:
        word = (prefix or suffix).lower()
        if word in lowered:
            continue
        candidate = " ".join(p for p in (prefix, base, suffix, "LLC") if p)
        candidate, err = sanitize_business_name(candidate)
        if err or len(candidate) > MAX_NAME_LENGTH or candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        out.append(candidate)
    return out


def _available_alternatives(cleaned_name: str) -> list:
    variants = suggest_variants(cleaned_name)[:ALTERNATIVES_TO_CHECK]
    if not variants:
        return []
    pool = cf.ThreadPoolExecutor(max_workers=len(variants))
    futures = {v: pool.submit(check_name_public, v) for v in variants}
    try:
        cf.wait(futures.values(), timeout=ALTERNATIVES_DEADLINE_SECONDS)
    finally:
        # Don't block on stragglers - whatever finished in time is enough.
        pool.shutdown(wait=False, cancel_futures=True)
    found = []
    for variant in variants:  # keep our preferred order, not completion order
        fut = futures[variant]
        if fut.done() and not fut.cancelled() and fut.exception() is None:
            if (fut.result() or {}).get("status") == "AVAILABLE":
                found.append(variant)
    return found[:MAX_ALTERNATIVES]


def check_name_with_alternatives(raw_name: str) -> dict:
    """Never raises. Returns {"ok": False, "error": ...} for an unusable name,
    otherwise {"ok": True, "name", "status" (AVAILABLE / TAKEN / UNAVAILABLE),
    "available" (True/False/None), "message", "conflicts", "alternatives"}."""
    cleaned, error = sanitize_business_name(normalize_input_name(raw_name))
    if error:
        return {"ok": False, "error": error}
    if len(cleaned) > MAX_NAME_LENGTH:
        return {"ok": False, "error": "That name is too long - please shorten it."}

    try:
        result = check_name_public(cleaned) or {}
    except Exception as e:  # check_name_public already swallows its own errors; belt and braces
        print(f"[name-check] check_name_public raised unexpectedly: {type(e).__name__}: {e}")
        result = {}

    status = result.get("status") or "UNAVAILABLE"
    out = {
        "ok": True,
        "name": cleaned,
        "status": status,
        "available": result.get("available"),
        "message": result.get("message") or "",
        "conflicts": [str(c) for c in (result.get("conflicts") or [])][:3],
        "alternatives": [],
    }
    if status == "TAKEN":
        try:
            out["alternatives"] = _available_alternatives(cleaned)
        except Exception as e:
            print(f"[name-check] alternatives lookup failed (non-fatal): {type(e).__name__}: {e}")
    return out
