"""One-time setup: creates the 'Launch Bridge Registered Agent Service' Stripe
Product and its $110.00/year recurring Price. Run once per Stripe mode
(test/staging vs live/production) - the resulting price ID gets pasted into
app/config.py's RA_ANNUAL_PRICE_ID for that environment.

Usage: APP_ENV=staging .venv/bin/python scripts/setup_ra_stripe_price.py
"""
import stripe
from app.config import STRIPE_SECRET_KEY, APP_ENV

stripe.api_key = STRIPE_SECRET_KEY

product = stripe.Product.create(
    name="Launch Bridge Registered Agent Service",
    description="Annual registered agent service provided by Christopher Shiplett, "
                 "a practicing Virginia attorney at Randolph Law, PLLC.",
)
price = stripe.Price.create(
    currency="usd",
    unit_amount=11000,
    recurring={"interval": "year"},
    product=product.id,
)

print(f"APP_ENV={APP_ENV}")
print(f"Product ID: {product.id}")
print(f"Price ID:   {price.id}")
