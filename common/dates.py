"""Date helpers and the payroll cut-off rule."""

from datetime import date
from datetime import datetime
import calendar

def current_month():
    """This calendar month as YYYY-MM: the month the app opens on."""
    return date.today().strftime("%Y-%m")

def parse_date(val):
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def fmt_date(d):
    return d.strftime("%Y-%m-%d") if d else None

def first_of_month(d):
    return date(d.year, d.month, 1)

def add_months(d, months):
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last_day))

def pay_month_label(payroll_month_str):
    return parse_date(payroll_month_str).strftime("%B %Y")

def get_payroll_month_for_event(event_date, cutoff_day=25):
    """Rule: Leave or travel dated after the 25th falls into next month's payroll."""
    if event_date.day > cutoff_day:
        return add_months(first_of_month(event_date), 1)
    return first_of_month(event_date)

def month_days(pm_str):
    pm = parse_date(pm_str)
    return [date(pm.year, pm.month, d) for d in range(1, calendar.monthrange(pm.year, pm.month)[1] + 1)]
