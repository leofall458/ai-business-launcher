"""Stock photos for generated customer websites, via the official Unsplash API.

Replaces loremflickr, which started answering every request with a 401
bot-check page - every stock image on every generated site and in the
backdrop picker came back blank. Unsplash needs an access key
(UNSPLASH_ACCESS_KEY, Secret Manager or .env); without one, or when the API
call fails, photo_url returns None and the website templates fall back to
their solid/gradient background - a missing image, never a broken one.

Unsplash's API terms, which this module follows:
  - hotlink the returned images.unsplash.com URLs (never re-host them);
  - ping the photo's download endpoint when a photo is actually used on a
    site (photo_credit);
  - credit the photographer and Unsplash (photo_credit, rendered in each
    template's footer).

The photo id rides along in the image URL's fragment (`#unsplash=<id>`):
browsers never send a fragment to the server, so the image still loads
normally, and whichever URL a customer picks in the backdrop picker (stored
on the order as a plain string) still says which photo it is.
"""

import functools
import re

import requests

from app.secrets import get_secret

API = "https://api.unsplash.com"
TIMEOUT_SECONDS = 8
_ID_FRAGMENT = re.compile(r"#unsplash=([A-Za-z0-9_-]+)$")
# Required by Unsplash on every link back to them / a photographer.
_UTM = "utm_source=launch_bridge&utm_medium=referral"


def _get(path: str, **params):
    key = get_secret("UNSPLASH_ACCESS_KEY")
    if not key:
        return None
    try:
        resp = requests.get(f"{API}{path}", params=params, timeout=TIMEOUT_SECONDS,
                            headers={"Authorization": f"Client-ID {key}", "Accept-Version": "v1"})
        if resp.status_code != 200:
            print(f"⚠️ Unsplash {path} returned {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.json()
    except Exception as e:
        print(f"⚠️ Unsplash {path} failed: {type(e).__name__}: {e}")
        return None


@functools.lru_cache(maxsize=256)
def _search(query: str) -> tuple:
    """Landscape photo results for `query`, as a tuple of (id, raw_url).
    Cached so the hero and About images of one site (same keywords, different
    index) cost a single API call - the free tier allows 50 requests/hour.
    Raises LookupError on no results/failure, which lru_cache never caches,
    so a transient API error is retried next time."""
    data = _get("/search/photos", query=query, per_page=10, orientation="landscape",
                content_filter="high")
    results = (data or {}).get("results") or []
    photos = tuple((p["id"], p["urls"]["raw"]) for p in results if p.get("id") and (p.get("urls") or {}).get("raw"))
    if not photos:
        raise LookupError(query)
    return photos


def photo_url(keywords: str, width: int, height: int, index: int = 0):
    """The index-th Unsplash photo for comma-separated `keywords`, cropped to
    width x height, or None if there's no key, no result, or the API failed."""
    query = " ".join(k.strip() for k in (keywords or "").split(",") if k.strip())
    if not query:
        return None
    try:
        photos = _search(query)
    except LookupError:
        return None
    photo_id, raw = photos[index % len(photos)]
    sep = "&" if "?" in raw else "?"
    return f"{raw}{sep}w={width}&h={height}&fit=crop&crop=entropy&q=75&fm=jpg#unsplash={photo_id}"


def photo_id_from_url(url: str):
    match = _ID_FRAGMENT.search(url or "")
    return match.group(1) if match else None


def is_stale_stock_url(url: str) -> bool:
    """URLs from the dead loremflickr integration, still stored on older
    orders (backdrop_image_options / backdrop_image_choice)."""
    return "loremflickr.com" in (url or "")


def photo_credit(url: str):
    """Looks up the photographer for an Unsplash URL from photo_url and
    records the use with Unsplash's download endpoint. Returns
    {"name", "profile_url", "unsplash_url"} for the site footer, or None."""
    photo_id = photo_id_from_url(url)
    if not photo_id:
        return None
    data = _get(f"/photos/{photo_id}")
    if not data:
        return None
    download = (data.get("links") or {}).get("download_location")
    if download:
        _get(download.removeprefix(API))
    user = data.get("user") or {}
    profile = (user.get("links") or {}).get("html")
    if not user.get("name") or not profile:
        return None
    return {
        "name": user["name"],
        "profile_url": f"{profile}?{_UTM}",
        "unsplash_url": f"https://unsplash.com/?{_UTM}",
    }
