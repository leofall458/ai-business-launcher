"""Short-lived, in-process holding spot for SSNs between /launch and /success.

The SSN is only needed once, to fill in the EIN filer after payment confirms.
It must never reach Firestore (the privacy policy promises it's never stored
in our database) - this dict is process memory only, gone on restart, and
each entry is discarded immediately after the EIN filer consumes it.
"""

_pending_ssns: dict[str, str] = {}

def stash(order_id: str, ssn: str):
    _pending_ssns[order_id] = ssn

def pop(order_id: str) -> str:
    return _pending_ssns.pop(order_id, "")
