import stripe
from app.config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, LLC_FORMATION_PRICE_CENTS

stripe.api_key = STRIPE_SECRET_KEY

def construct_webhook_event(payload: bytes, sig_header: str):
    """Verifies the Stripe-Signature header against the raw request body
    and the webhook's signing secret - raises if the payload was tampered
    with or didn't actually come from Stripe. Must be called with the raw
    bytes (not re-serialized JSON), since the signature is computed over
    the exact bytes Stripe sent."""
    return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)

def create_checkout_session(
    order_id: str, success_url: str, cancel_url: str,
    amount: int = None, founding_member: bool = False, email: str = None,
):
    """Stripe Checkout Session for the flat-fee LLC formation package.
    order_id travels in metadata + client_reference_id so /success can look
    up the right Firestore order regardless of which one it reads back.
    amount defaults to LLC_FORMATION_PRICE_CENTS ($350); pass a lower value
    for the founding-member discount.

    Called at Step 2, before the customer has picked a business name (that's
    Step 3, post-payment) - so the line item can't be named after the
    business. customer_email/phone_number_collection let Stripe's own hosted
    form collect contact info; email is only ever passed if already known
    (e.g. a resumed abandoned checkout) since Stripe rejects an empty string.
    Checkout renders Apple Pay/Google Pay automatically above the card form
    once wallets are enabled in the Stripe Dashboard - no extra params
    needed for that."""
    charge = amount if amount is not None else LLC_FORMATION_PRICE_CENTS
    if founding_member:
        description = (
            "$150 Launch Bridge service fee (Founding Member rate — $100 off) + "
            "$100 Virginia state filing fee = $250 total. "
            "Full service: LLC filing, EIN, brand kit, marketing plan, website, and Stripe setup."
        )
        product_name = "Virginia LLC Formation — Founding Member"
    else:
        description = (
            "$250 Launch Bridge service fee + $100 Virginia state filing fee "
            "(we pay this to Virginia for you) = $350 total. "
            "Includes EIN application, brand kit, marketing plan, and business website."
        )
        product_name = "Virginia LLC Formation"
    kwargs = {}
    if email:
        kwargs["customer_email"] = email
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
        metadata={"order_id": order_id, "founding_member": "true" if founding_member else "false"},
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
