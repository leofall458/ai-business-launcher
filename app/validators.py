"""Server-side validation for the /launch intake form. Runs before anything
touches Firestore or Stripe - a Checkout Session or an order document
created from bad data (fake SSN, undeliverable email, non-VA address) is
expensive to unwind, so we reject it up front instead.
"""

import re
import datetime
import dns.resolver
import phonenumbers

# Virginia doesn't collect a "state" field on the intake form at all - the
# product is VA-only by construction, so there's nothing to validate there.
VA_ZIP_RANGES = [(20100, 20199), (22000, 24699)]

NAME_RE = re.compile(r"[A-Za-z'\- ]+")
CITY_RE = re.compile(r"[A-Za-z ]+")
BUSINESS_NAME_RE = re.compile(r"[A-Za-z ,.'\-]+")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Well-known placeholder/example SSNs that show up in test data and scams,
# on top of the structural rules below.
FAKE_SSNS = {"123456789", "078051120", "219099999", "457555462"}

# IRS EIN prefixes that were never issued - see IRS Pub 1635.
EIN_INVALID_PREFIXES = {
    "00", "07", "08", "09", "17", "18", "19", "28", "29",
    "49", "69", "70", "78", "79", "89",
}

ALL_VALIDATED_FIELDS = [
    "first_name", "last_name", "email", "phone", "dob",
    "address", "city", "zipcode", "desired_name",
    "existing_llc_name", "existing_ein",
    "photo_1", "photo_2", "photo_3",
]

# Used by the new payment-first wizard (/launch only collects these).
PRE_PAYMENT_VALIDATED_FIELDS = [
    "first_name", "last_name", "email", "phone",
    "desired_name", "existing_llc_name",
]

# Used by /start - business_idea moved to post-payment (Step 1, name
# selection) so customers can pay before answering any questions; consent
# is the only thing still validated pre-payment.
CHECKOUT_VALIDATED_FIELDS = ["consent"]

# Used by the post-payment dashboard intake form.
POST_PAYMENT_VALIDATED_FIELDS = [
    "address", "city", "zipcode", "county",
    "sig_first", "sig_last", "existing_ein",
    "photo_1", "photo_2", "photo_3",
]

def validate_name(value: str, label: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return f"{label} is required."
    if not (2 <= len(value) <= 50):
        return f"{label} must be between 2 and 50 characters."
    if not NAME_RE.fullmatch(value):
        return f"{label} can only contain letters, spaces, hyphens, and apostrophes."
    return None

def validate_address(address: str) -> str | None:
    address = (address or "").strip()
    if not address:
        return "Street address is required."
    if len(address) < 5:
        return "Street address must be at least 5 characters."
    return None

def validate_city(city: str) -> str | None:
    city = (city or "").strip()
    if not city:
        return "City is required."
    if not CITY_RE.fullmatch(city):
        return "City must contain letters only."
    return None

def validate_zip(zip_str: str) -> str | None:
    zip_str = (zip_str or "").strip()
    if not re.fullmatch(r"\d{5}", zip_str):
        return "ZIP code must be exactly 5 digits."
    z = int(zip_str)
    if not any(lo <= z <= hi for lo, hi in VA_ZIP_RANGES):
        return "ZIP code must be a valid Virginia ZIP code."
    return None

def validate_ssn(ssn: str) -> str | None:
    ssn = (ssn or "").strip()
    if not re.fullmatch(r"\d{3}-\d{2}-\d{4}", ssn):
        return "SSN must be in the format XXX-XX-XXXX."

    area, group, serial = ssn.split("-")
    if area == "000" or area == "666" or 900 <= int(area) <= 999:
        return "Invalid SSN - the first 3 digits cannot be 000, 666, or 900-999."
    if group == "00":
        return "Invalid SSN - the middle 2 digits cannot be 00."
    if serial == "0000":
        return "Invalid SSN - the last 4 digits cannot be 0000."

    digits = area + group + serial
    if digits in FAKE_SSNS or digits == digits[0] * 9:
        return "That SSN looks like a placeholder, not a real one - please enter your actual SSN."
    return None

def validate_email(email: str) -> str | None:
    email = (email or "").strip()
    if not EMAIL_RE.fullmatch(email):
        return "Enter a valid email address."

    domain = email.rsplit("@", 1)[1]
    try:
        dns.resolver.resolve(domain, "MX")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return "That email domain doesn't accept mail - check for typos."
    except Exception as e:
        # Transient DNS/network failure, not proof the domain is bad - don't
        # block checkout over a flaky resolver.
        print(f"⚠️ MX lookup for {domain} failed ({e}) - allowing through.")
    return None

def validate_phone(phone: str) -> str | None:
    phone = (phone or "").strip()
    try:
        parsed = phonenumbers.parse(phone, "US")
    except phonenumbers.NumberParseException:
        return "Enter a valid 10-digit US phone number."

    if parsed.country_code != 1 or not phonenumbers.is_valid_number_for_region(parsed, "US"):
        return "Enter a valid 10-digit US phone number."
    if len(str(parsed.national_number)) != 10:
        return "Phone number must be 10 digits."
    return None

def validate_dob(dob_str: str) -> str | None:
    try:
        dob = datetime.date.fromisoformat((dob_str or "").strip())
    except ValueError:
        return "Enter a valid date of birth."

    today = datetime.date.today()
    if dob > today:
        return "Date of birth cannot be in the future."

    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < 18:
        return "You must be at least 18 years old to form an LLC."
    return None

def validate_business_name(name: str) -> str | None:
    name = (name or "").strip()
    if not (3 <= len(name) <= 100):
        return "Business name must be between 3 and 100 characters."
    if not BUSINESS_NAME_RE.fullmatch(name):
        return "Business name can only contain letters, spaces, hyphens, apostrophes, commas, and periods."
    if not re.search(r"LLC\.?$", name, re.IGNORECASE):
        return 'Business name must end with "LLC".'
    if re.fullmatch(r"LLC\.?", name, re.IGNORECASE):
        return 'Business name cannot be just "LLC" - include your actual business name.'
    return None

def validate_ein(ein: str) -> str | None:
    ein = (ein or "").strip()
    if not re.fullmatch(r"\d{2}-\d{7}", ein):
        return "EIN must be in the format XX-XXXXXXX."
    prefix = ein[:2]
    if prefix in EIN_INVALID_PREFIXES:
        return f"Invalid EIN - the first 2 digits cannot be {prefix}."
    return None

def validate_business_idea(idea: str) -> str | None:
    idea = (idea or "").strip()
    if not idea:
        return "Please describe your business idea."
    if len(idea) < 10:
        return "Please give a bit more detail about your business idea."
    if len(idea) > 1000:
        return "That's a lot of detail — please keep it under 1000 characters."
    return None


def validate_pre_payment_form(form: dict) -> dict:
    """Validates only the 5 fields collected before payment in the wizard."""
    errors = {}

    def check(field, fn, *args):
        err = fn(*args)
        if err:
            errors[field] = err

    skip_llc = form.get("skip_llc_formation") == "on"
    check("first_name", validate_name, form.get("first_name"), "First name")
    check("last_name", validate_name, form.get("last_name"), "Last name")
    check("email", validate_email, form.get("email"))
    check("phone", validate_phone, form.get("phone"))

    if skip_llc:
        check("existing_llc_name", validate_business_name, form.get("existing_llc_name"))
    else:
        check("desired_name", validate_business_name, form.get("desired_name"))

    return errors


def validate_step4_details(form: dict) -> dict:
    """Validates Step 4 (personal info) in the post-payment dashboard:
    name/phone/dob plus the Virginia address, split out of the old
    single-page post-payment intake form so address collection can happen
    before business details (Step 5). Email isn't collected here - it's
    captured from Stripe Checkout at payment time (see process_paid_order
    in app/main.py) and isn't shown again on this step."""
    errors = {}

    def check(field, fn, *args):
        err = fn(*args)
        if err:
            errors[field] = err

    check("first_name", validate_name, form.get("first_name"), "First name")
    check("last_name", validate_name, form.get("last_name"), "Last name")
    check("phone", validate_phone, form.get("phone"))
    check("dob", validate_dob, form.get("dob"))
    check("address", validate_address, form.get("address"))
    check("city", validate_city, form.get("city"))
    check("zipcode", validate_zip, form.get("zipcode"))

    if not (form.get("county") or "").strip():
        errors["county"] = "County is required for your EIN application."

    return errors


def validate_post_payment_intake(form: dict) -> dict:
    """Validates Step 5 (business details) in the post-payment dashboard:
    LLC member signatures and the EIN-skip toggle. Address/city/zip/county
    are Step 4's job (validate_step4_details above) - already on the order
    by the time Step 5 submits."""
    errors = {}

    def check(field, fn, *args):
        err = fn(*args)
        if err:
            errors[field] = err

    check("sig_first", validate_name, form.get("sig_first"), "First name (signature)")
    check("sig_last", validate_name, form.get("sig_last"), "Last name (signature)")

    if form.get("skip_ein") == "on":
        check("existing_ein", validate_ein, form.get("existing_ein"))

    if form.get("consent_signature") != "on":
        errors["consent_signature"] = "Please agree to the terms to continue"

    return errors


def validate_intake_form(form: dict) -> dict:
    """Returns a dict of field_name -> error message for every field that
    failed validation. Empty dict means the form is clean.

    skip_llc_formation and skip_ein swap which fields are actually
    required: a customer with an existing LLC/EIN doesn't fill in
    desired_name/ssn at all, so validating those instead of the existing_*
    fields would reject a perfectly valid submission."""
    errors = {}

    def check(field, fn, *args):
        err = fn(*args)
        if err:
            errors[field] = err

    skip_llc = form.get("skip_llc_formation") == "on"
    skip_ein = form.get("skip_ein") == "on"

    check("first_name", validate_name, form.get("first_name"), "First name")
    check("last_name", validate_name, form.get("last_name"), "Last name")
    check("email", validate_email, form.get("email"))
    check("phone", validate_phone, form.get("phone"))
    check("dob", validate_dob, form.get("dob"))
    check("address", validate_address, form.get("address"))
    check("city", validate_city, form.get("city"))
    check("zipcode", validate_zip, form.get("zipcode"))

    if skip_ein:
        check("existing_ein", validate_ein, form.get("existing_ein"))
    else:
        check("ssn", validate_ssn, form.get("ssn"))

    if skip_llc:
        check("existing_llc_name", validate_business_name, form.get("existing_llc_name"))
    else:
        check("desired_name", validate_business_name, form.get("desired_name"))

    return errors
