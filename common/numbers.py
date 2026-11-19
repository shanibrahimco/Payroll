"""Checks for numbers typed into forms."""

import math

from common.errors import ChangeError

MAX_FORM_NUMBER = 1_000_000_000  # no amount, quantity or rate in this system comes near this

def checked_number(value, error="bad_input", default=0.0):
    """A number from a form. Blank gives the default; text, infinity and absurdly large values are refused with `error`."""
    if value is None or str(value).strip() == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ChangeError(error)
    if not math.isfinite(number) or abs(number) > MAX_FORM_NUMBER:
        raise ChangeError(error)
    return number
