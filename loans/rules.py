"""Loan and advance policy."""

from datetime import date

from common.dates import add_months, first_of_month, fmt_date, parse_date
from payroll.calculator import effective_salary

def loan_balance(ln, as_of):
    """What is still to be recovered on one advances_loans row at the start of the month `as_of` (same rules as the payroll)."""
    amt = float(ln["amount"])
    inst = amt if ln["loan_type"] == "Advance" else float(ln["monthly_instalment"] or 0)
    first_rec = parse_date(ln["first_recovery_month"])
    recovered = 0.0
    if first_rec and as_of >= first_rec:
        elapsed = (as_of.year - first_rec.year) * 12 + (as_of.month - first_rec.month)
        recovered = min(amt, inst * max(0, elapsed - int(ln["months_on_hold_prior"] or 0)))
    return max(0.0, amt - recovered)

def loan_outstanding(conn, emp_id, as_of_month):
    """Loans / advances still to be recovered at the start of `as_of_month` (same recovery rules as the payroll),
    plus loan applications still waiting for a decision."""
    as_of = parse_date(as_of_month)
    total = 0.0
    for ln in conn.execute("SELECT * FROM advances_loans WHERE emp_id = ?", (emp_id,)):
        total += loan_balance(ln, as_of)
    total += float(conn.execute("SELECT COALESCE(SUM(amount), 0) FROM loan_applications WHERE emp_id = ? AND status IN ('Applied', 'Recommended')",
                                (emp_id,)).fetchone()[0])
    return total

def loan_policy(conn, staff_row, amount, as_of_month, cfg):
    """(limit, outstanding, exceeds): outstanding loans + this amount may not exceed `loan_max_salaries` salaries."""
    limit = effective_salary(conn, staff_row, as_of_month, cfg) * float(cfg.get("loan_max_salaries", 2))
    outstanding = loan_outstanding(conn, staff_row["emp_id"], as_of_month)
    return limit, outstanding, outstanding + amount > limit

def approve_loan_application(conn, app_row, amount, user, now):
    """Create the advances_loans record: recovered from the month after approval, in equal instalments."""
    import math
    ref = f"LA{app_row['id']:04d}"
    approved_on = date.today()
    instalment = amount if app_row["loan_type"] == "Advance" else math.ceil(amount / app_row["instalments"])
    conn.execute("""INSERT INTO advances_loans (loan_ref, emp_id, loan_type, date_given, amount, monthly_instalment, first_recovery_month,
                                               purpose, no_of_instalments, recommended_by, approved_by, approval_date, remarks, disbursed)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                 (ref, app_row["emp_id"], app_row["loan_type"], fmt_date(approved_on), amount, instalment,
                  fmt_date(add_months(first_of_month(approved_on), 1)), app_row["purpose"], app_row["instalments"],
                  app_row["reviewed_by"], user, fmt_date(approved_on), f"Applied online (application #{app_row['id']})"
                  + (" — approved as exception to loan policy" if app_row["exceeds_policy"] else "")))
    return ref, instalment
