import stripe
from app.config import STRIPE_SECRET_KEY, LLC_FORMATION_PRICE_CENTS

stripe.api_key = STRIPE_SECRET_KEY

def create_checkout_session(order_id: str, business_name: str, success_url: str, cancel_url: str):
    """Stripe Checkout Session for the flat-fee LLC formation package.
    order_id travels in metadata + client_reference_id so /success can look
    up the right Firestore order regardless of which one it reads back."""
    return stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": LLC_FORMATION_PRICE_CENTS,
                "product_data": {
                    "name": f"Virginia LLC Formation - {business_name}",
                    "description": "Virginia LLC Formation - includes state filing fee, EIN application, brand kit, marketing plan, and business website",
                },
            },
            "quantity": 1,
        }],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=order_id,
        metadata={"order_id": order_id},
    )

def retrieve_checkout_session(session_id: str):
    return stripe.checkout.Session.retrieve(session_id)

def create_connect_account(email: str, first_name: str, last_name: str, business_name: str, multi_member: bool):
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
    legacy type="standard" field fails the same way type="express" did."""
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
        individual={
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
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
