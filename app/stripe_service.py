import stripe
from google.cloud import firestore
from app.config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, LLC_FORMATION_PRICE_CENTS, FIREBASE_PROJECT_ID, ORDERS_COLLECTION

stripe.api_key = STRIPE_SECRET_KEY

# Own Firestore client (same pattern as app/dashboard_auth.py) so
# check_and_update_website can look up an order without app.main having to
# import this module in a circular loop - main.py already imports several
# functions from here at module load time.
_db = firestore.Client(project=FIREBASE_PROJECT_ID)
_ORDERS = _db.collection(ORDERS_COLLECTION)

def construct_webhook_event(payload: bytes, sig_header: str):
    """Verifies the Stripe-Signature header against the raw request body
    and the webhook's signing secret - raises if the payload was tampered
    with or didn't actually come from Stripe. Must be called with the raw
    bytes (not re-serialized JSON), since the signature is computed over
    the exact bytes Stripe sent."""
    return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)

def create_checkout_session(
    order_id: str, success_url: str, cancel_url: str,
    amount: int = None, email: str = None,
    gclid: str = None, utm_source: str = None, utm_medium: str = None, utm_campaign: str = None,
):
    """Stripe Checkout Session for the flat-fee LLC formation package.
    order_id travels in metadata + client_reference_id so /success can look
    up the right Firestore order regardless of which one it reads back.
    amount defaults to LLC_FORMATION_PRICE_CENTS ($350).

    Called at Step 2, before the customer has picked a business name (that's
    Step 3, post-payment) - so the line item can't be named after the
    business. customer_email/phone_number_collection let Stripe's own hosted
    form collect contact info; email is only ever passed if already known
    (e.g. a resumed abandoned checkout) since Stripe rejects an empty string.
    Checkout renders Apple Pay/Google Pay automatically above the card form
    once wallets are enabled in the Stripe Dashboard - no extra params
    needed for that.

    gclid/utm_* are whatever the order already has stored from the
    lb_attribution cookie (see parse_attribution_cookie in main.py) - passed
    straight through, not re-derived here. Stripe metadata values must be
    strings, so a missing/null one is simply left out of the dict rather
    than sent as None - Checkout still creates normally either way."""
    charge = amount if amount is not None else LLC_FORMATION_PRICE_CENTS
    description = (
        "$250 Launch Bridge service fee + $100 Virginia state filing fee "
        "(we pay this to Virginia for you) = $350 total. "
        "Includes EIN application, brand kit, marketing plan, and business website."
    )
    product_name = "Virginia LLC Formation"
    kwargs = {}
    if email:
        kwargs["customer_email"] = email
    metadata = {"order_id": order_id}
    for key, value in (
        ("gclid", gclid), ("utm_source", utm_source),
        ("utm_medium", utm_medium), ("utm_campaign", utm_campaign),
    ):
        if value:
            metadata[key] = value
    return stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": charge,
                "product_data": {
                    "name": product_name,
                    "description": description,
                },
            },
            "quantity": 1,
        }],
        phone_number_collection={"enabled": True},
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=order_id,
        metadata=metadata,
        **kwargs,
    )

def retrieve_checkout_session(session_id: str):
    return stripe.checkout.Session.retrieve(session_id)

def create_connect_account(email: str, business_name: str, multi_member: bool):
    """Standard account for the customer, pre-filled as an LLC. Stripe's
    company.structure enum is what actually encodes "LLC" (there's no
    bare business_type value for it - business_type is the broader
    "company" vs "individual" split).

    Standard accounts are full, independent Stripe accounts the customer
    owns and manages themselves (their own dashboard, their own fees and
    chargeback liability) - the platform just gets API access via
    stripe_account, vs. Express where the platform manages billing/payouts
    on the customer's behalf. Uses the Accounts v2 controller shape since
    this platform's Connect setup is provisioned for that model - the
    legacy type="standard" field fails the same way type="express" did.

    Stripe rejects a top-level `individual` block on business_type="company"
    accounts - that field only exists for sole-proprietor (business_type=
    "individual") accounts. The LLC's representative gets collected by
    Stripe's own onboarding flow (see create_account_link) instead."""
    return stripe.Account.create(
        controller={
            "stripe_dashboard": {"type": "full"},
            "fees": {"payer": "account"},
            "losses": {"payments": "stripe"},
        },
        country="US",
        email=email,
        business_type="company",
        company={
            "name": business_name,
            "structure": "multi_member_llc" if multi_member else "single_member_llc",
        },
    )

def create_account_link(account_id: str, refresh_url: str, return_url: str) -> str:
    """Stripe account links expire a few minutes after creation - there is
    no API option for a 24-hour-valid link. Generate one fresh, on demand,
    right when the customer is about to use it (see /connect/onboard)."""
    link = stripe.AccountLink.create(
        account=account_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )
    return link.url

def is_account_active(account_id: str) -> bool:
    """True once the customer has finished Stripe's onboarding and the
    account can actually accept charges - used to tell "Complete Setup"
    apart from "Active" on the status page."""
    try:
        account = stripe.Account.retrieve(account_id)
        return bool(account.charges_enabled)
    except Exception:
        return False

def create_pay_what_you_want_payment_link(connect_account_id: str, business_name: str) -> str:
    """A generic 'pay this business' link that settles directly to the
    customer's own connected account, not the platform. Since we don't know
    what they actually sell, the price is customer-entered (Stripe's
    custom_unit_amount / 'pay what you want' price type)."""
    product = stripe.Product.create(
        name=f"Payment to {business_name}",
        stripe_account=connect_account_id,
    )
    price = stripe.Price.create(
        currency="usd",
        custom_unit_amount={"enabled": True, "minimum": 500},
        product=product.id,
        stripe_account=connect_account_id,
    )
    payment_link = stripe.PaymentLink.create(
        line_items=[{"price": price.id, "quantity": 1}],
        stripe_account=connect_account_id,
    )
    return payment_link.url

def check_and_update_website(order_id: str) -> dict:
    """Checks whether a customer's connected Stripe account has finished
    onboarding (charges_enabled) and, if so, makes sure their live website
    actually shows a working payment button - not just that Firestore has
    a payment link on file.

    Reuses create_pay_what_you_want_payment_link (a customer-priced "pay
    what you want" link scoped to the customer's own connected account,
    already used elsewhere in this codebase) rather than creating a
    separate fixed-price Payment Link on the platform account - a flat,
    unexplained $100 charge makes no sense for most of these businesses,
    and having two different payment-link mechanisms for the same button
    would be confusing and easy to get out of sync.

    Idempotent via the payment_button_live flag: once the website has
    actually been regenerated with a real payment link, later calls (e.g.
    the hourly scheduler, or the admin's manual check) are a fast no-op
    instead of re-running Gemini + a Firebase deploy every time.

    Returns {"updated": bool, "url"/"reason": ...} - callers (the hourly
    scheduler, the admin "Check Stripe Status" button) use this to report
    what happened without needing to know the internals."""
    order_ref = _ORDERS.document(order_id)
    order = order_ref.get().to_dict()
    if not order:
        return {"updated": False, "reason": "Order not found."}

    connect_id = order.get("stripe_connect_account_id")
    if not connect_id:
        return {"updated": False, "reason": "No Stripe account yet."}

    if order.get("payment_button_live"):
        return {"updated": False, "reason": "Already up to date."}

    if not is_account_active(connect_id):
        return {"updated": False, "reason": "Customer hasn't completed Stripe setup yet."}

    payment_link_url = order.get("stripe_payment_link_url")
    if not payment_link_url:
        try:
            payment_link_url = create_pay_what_you_want_payment_link(connect_id, order.get("business_name", ""))
            order_ref.set({"stripe_payment_link_url": payment_link_url}, merge=True)
            order["stripe_payment_link_url"] = payment_link_url
        except Exception as e:
            return {"updated": False, "reason": f"Could not create payment link: {e}"}

    # Deferred import: app.main imports several functions from this module
    # at load time, so importing it back at the top of this file would be
    # a circular import. By the time this function actually runs, both
    # modules have finished loading.
    from app.main import run_website_regeneration
    result = run_website_regeneration(order_id)
    if not result.get("success"):
        return {"updated": False, "reason": result.get("error", "Website regeneration failed.")}

    order_ref.set({"payment_button_live": True}, merge=True)
    from app.email_service import send_payment_button_live_email
    try:
        send_payment_button_live_email(order, order_id, result["url"])
    except Exception as e:
        print(f"⚠️ Could not send payment-button-live email for order {order_id}: {e}")

    return {"updated": True, "url": result["url"]}
