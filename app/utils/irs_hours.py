"""The IRS EIN online assistant is only available Monday-Friday, 7:00 AM -
10:00 PM Eastern Time. Outside that window it's not a filing failure, it's
just closed - this module is the single source of truth for that schedule
so the order pipeline, admin dashboard, and customer status page all agree
on when the EIN step can run and when it'll next be available.
"""

from datetime import datetime, timedelta
import pytz

EASTERN = pytz.timezone("America/New_York")

def is_irs_open(now: datetime = None) -> bool:
    """True if `now` (or the current time) falls Mon-Fri 7am-10pm Eastern."""
    now = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    return now.weekday() < 5 and 7 <= now.hour < 22

def next_irs_open(now: datetime = None) -> datetime:
    """Next Eastern-time moment the IRS assistant opens, assuming `now` is
    currently outside hours. Gives customers/admins a real, specific time
    instead of a vague "try later"."""
    now = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    candidate = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate = (candidate + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate

def format_eta(dt: datetime) -> str:
    """e.g. "Monday, June 23 at 7:00 AM Eastern" """
    return f"{dt.strftime('%A, %B %d')} at {dt.strftime('%I:%M %p').lstrip('0')} Eastern"
