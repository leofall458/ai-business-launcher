"""Defense-in-depth for SSN handling: no code path should ever print a raw
SSN, but anything printed near the collect-ssn route or the vault's own
error handling runs through here first in case an underlying exception
message ever echoes one back."""

import re

_SSN_PATTERN = re.compile(r"\d{3}-\d{2}-\d{4}")

def scrub_ssn(message: str) -> str:
    return _SSN_PATTERN.sub("[REDACTED]", message)
