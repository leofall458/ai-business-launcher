"""Server-side Google Ads offline conversion import - reports a completed
Stripe payment back to Google Ads against the gclid that brought the
customer in, via the Click Conversion Upload API. Triggered from the
Stripe webhook (see stripe_webhook/import_ad_conversion in app/main.py),
not /success - the webhook is the server-authoritative record of payment,
independent of the customer's browser/ad-blocker. This is now the source
of truth for ad attribution; the existing client-side gtag('event',
'purchase') calls are untouched and keep firing for GA4 visibility.

Only ever calls the real API in production - staging pays with Stripe test
keys and synthetic gclids, and importing those as real conversions would
pollute the live Google Ads account's data. On any other APP_ENV this just
logs what it would have sent and returns False.

Every credential below is optional at import time - a blank
GOOGLE_ADS_CONVERSION_ACTION (etc.) just means upload_click_conversion()
skips with a clear log line instead of crashing, since none of this can
work until real values are in Secret Manager (see MANAGED_SECRETS in
app/secrets.py)."""

import datetime
from app.config import (
    APP_ENV,
    GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET,
    GOOGLE_ADS_REFRESH_TOKEN, GOOGLE_ADS_LOGIN_CUSTOMER_ID, GOOGLE_ADS_CUSTOMER_ID,
    GOOGLE_ADS_CONVERSION_ACTION,
)

def _is_configured() -> bool:
    return bool(
        GOOGLE_ADS_DEVELOPER_TOKEN and GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET
        and GOOGLE_ADS_REFRESH_TOKEN and GOOGLE_ADS_CUSTOMER_ID and GOOGLE_ADS_CONVERSION_ACTION
    )

def _get_client():
    # Imported lazily so a missing/broken google-ads install can't break
    # module import for the rest of the app - only this function's callers
    # (which already only run when _is_configured() is true) ever need it.
    from google.ads.googleads.client import GoogleAdsClient
    config = {
        "developer_token": GOOGLE_ADS_DEVELOPER_TOKEN,
        "client_id": GOOGLE_ADS_CLIENT_ID,
        "client_secret": GOOGLE_ADS_CLIENT_SECRET,
        "refresh_token": GOOGLE_ADS_REFRESH_TOKEN,
        "use_proto_plus": True,
    }
    if GOOGLE_ADS_LOGIN_CUSTOMER_ID:
        config["login_customer_id"] = GOOGLE_ADS_LOGIN_CUSTOMER_ID
    return GoogleAdsClient.load_from_dict(config)

def _format_conversion_datetime(dt: datetime.datetime) -> str:
    """Google Ads wants "yyyy-MM-dd HH:mm:ss+|-HH:MM" - close to but not
    quite ISO 8601 (space instead of "T", and the UTC offset needs a colon
    that %z alone doesn't reliably add across Python versions), so this is
    built by hand rather than trusting strftime/isoformat directly."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    offset = dt.strftime("%z")  # e.g. "+0000"
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f"{offset[:3]}:{offset[3:]}"

def upload_click_conversion(
    gclid: str, order_id: str, conversion_value: float,
    conversion_datetime: datetime.datetime, currency_code: str = "USD",
) -> bool:
    """Reports one completed purchase to Google Ads against its gclid.
    Never raises - the caller must never let an ad-platform hiccup affect
    the webhook's core job of marking the order paid. Returns True only if
    Google Ads actually accepted the conversion (a request-level exception,
    a per-conversion partial-failure rejection, missing gclid, unconfigured
    credentials, and non-production APP_ENV all return False instead)."""
    if not gclid:
        return False
    if APP_ENV != "production":
        print(f"[google_ads] Skipping conversion import on {APP_ENV} "
              f"(order {order_id}, gclid={gclid}, value={conversion_value}) - not production.")
        return False
    if not _is_configured():
        print(f"⚠️ Google Ads conversion import not configured (missing credentials) - skipping order {order_id}")
        return False

    try:
        client = _get_client()
        conversion_upload_service = client.get_service("ConversionUploadService")

        click_conversion = client.get_type("ClickConversion")
        click_conversion.gclid = gclid
        click_conversion.conversion_action = GOOGLE_ADS_CONVERSION_ACTION
        click_conversion.conversion_date_time = _format_conversion_datetime(conversion_datetime)
        click_conversion.conversion_value = conversion_value
        click_conversion.currency_code = currency_code
        click_conversion.order_id = order_id

        request = client.get_type("UploadClickConversionsRequest")
        request.customer_id = GOOGLE_ADS_CUSTOMER_ID
        request.conversions = [click_conversion]
        # partial_failure=True means a rejected conversion comes back as a
        # normal response with partial_failure_error set, rather than the
        # whole call raising - checked explicitly below since we're only
        # ever uploading one conversion per call anyway.
        request.partial_failure = True

        response = conversion_upload_service.upload_click_conversions(request=request)
        if response.partial_failure_error and response.partial_failure_error.message:
            print(f"⚠️ Google Ads rejected conversion for order {order_id}: {response.partial_failure_error.message}")
            return False
        print(f"✅ Google Ads conversion imported for order {order_id} (gclid={gclid}, value={conversion_value})")
        return True
    except Exception as e:
        print(f"⚠️ Google Ads conversion import failed for order {order_id}: {e}")
        return False
