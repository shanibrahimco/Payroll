"""Staff rules: EOBI start date and CNIC expiry warnings."""

from datetime import date

from common.constants import ARTICLE_TYPES, CNIC_WARN_DAYS
from common.dates import add_months, parse_date

def eobi_start_date(staff_type, date_of_joining, icap_reg_date, eobi_reg_date=None, permanent_wait_months=3):
    """Rule: Interns never get EOBI. Anyone else already enrolled with EOBI starts from the
    EOBI registration date. Otherwise EOBI starts for Permanent staff after the waiting period
    from joining, and for ICAP Articles from the date ICAP confirms registration.
    Returns None when EOBI does not (yet) apply."""
    if staff_type == "Intern":
        return None
    if eobi_reg_date:
        return eobi_reg_date
    if staff_type == "Permanent":
        return add_months(date_of_joining, permanent_wait_months) if date_of_joining else None
    if staff_type in ARTICLE_TYPES:
        return icap_reg_date
    return None

def cnic_alert(expiry, today=None):
    """'' if fine, else a short warning for an expired / soon-expiring CNIC."""
    d = parse_date(expiry)
    if d is None:
        return ""
    today = today or date.today()
    if d < today:
        return f"CNIC expired on {d.strftime('%d %b %Y')}"
    if (d - today).days <= CNIC_WARN_DAYS:
        return f"CNIC expires on {d.strftime('%d %b %Y')}"
    return ""
