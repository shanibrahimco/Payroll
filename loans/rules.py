"""Loan and advance policy."""

from datetime import date

from common.dates import add_months, first_of_month, fmt_date, parse_date
from common.errors import ChangeError
from common.numbers import checked_number
from payroll.calculator import effective_salary

INSTALMENTS = "Instalments"  # recovered from salary each month
LUMP_SUM = "Lump sum"  # repaid in one payment by a date the staff member commits to; nothing is deducted from salary

def loan_repaid(conn, loan_ref, up_to=None):
    """Money paid back directly on a loan (cash or bank, outside the payroll), up to and including `up_to` if given."""
    sql = "SELECT COALESCE(SUM(amount), 0) FROM loan_repayments WHERE loan_ref = ?" + (" AND paid_on <= ?" if up_to else "")
    return float(conn.execute(sql, (loan_ref, up_to) if up_to else (loan_ref,)).fetchone()[0])

def loan_balance(ln, as_of, repaid=0.0):
    """What is still to be recovered on one advances_loans row at the start of the month `as_of` (same rules as the payroll),
    less anything paid back directly."""
    amt = float(ln["amount"])
    inst = amt if ln["loan_type"] == "Advance" else float(ln["monthly_instalment"] or 0)
    first_rec = parse_date(ln["first_recovery_month"])
    recovered = 0.0
    if first_rec and as_of >= first_rec:
        elapsed = (as_of.year - first_rec.year) * 12 + (as_of.month - first_rec.month)
        recovered = min(amt, inst * max(0, elapsed - int(ln["months_on_hold_prior"] or 0)))
    return max(0.0, amt - recovered - repaid)

def loan_outstanding(conn, emp_id, as_of_month):
    """Loans / advances still to be recovered at the start of `as_of_month` (same recovery rules as the payroll),
    plus loan applications still waiting for a decision."""
    as_of = parse_date(as_of_month)
    total = 0.0
    for ln in conn.execute("SELECT * FROM advances_loans WHERE emp_id = ?", (emp_id,)).fetchall():
        total += loan_balance(ln, as_of, loan_repaid(conn, ln["loan_ref"]))
    total += float(conn.execute("SELECT COALESCE(SUM(amount), 0) FROM loan_applications WHERE emp_id = ? AND status IN ('Applied', 'Recommended')",
                                (emp_id,)).fetchone()[0])
    return total

def loan_policy(conn, staff_row, amount, as_of_month, cfg):
    """(limit, outstanding, exceeds): outstanding loans + this amount may not exceed `loan_max_salaries` salaries."""
    limit = effective_salary(conn, staff_row, as_of_month, cfg) * float(cfg.get("loan_max_salaries", 2))
    outstanding = loan_outstanding(conn, staff_row["emp_id"], as_of_month)
    return limit, outstanding, outstanding + amount > limit

def lump_sum_date(text, cfg):
    """The committed repayment date of a lump-sum loan: after today, and no later than the longest instalment loan allowed."""
    due = parse_date(text)
    latest = add_months(date.today(), int(float(cfg.get("loan_max_instalments", 20))))
    if due is None or due <= date.today() or due > latest:
        raise ChangeError("loan_lump_date")
    return due

def approve_loan_application(conn, app_row, amount, user, now):
    """Create the advances_loans record: recovered from the month after approval in equal instalments, or (lump sum)
    not deducted from salary at all and repaid in one payment by the committed date."""
    import math
    ref = f"LA{app_row['id']:04d}"
    approved_on = date.today()
    lump = app_row["loan_type"] == "Loan" and app_row["repayment_method"] == LUMP_SUM
    if lump:
        due = parse_date(app_row["lump_sum_date"])
        instalment, first_recovery, instalments = 0, fmt_date(first_of_month(due)), None
        remarks = f"Applied online (application #{app_row['id']}); repaid in one lump sum by {fmt_date(due)}"
    else:
        instalment = amount if app_row["loan_type"] == "Advance" else math.ceil(amount / app_row["instalments"])
        first_recovery, instalments = fmt_date(add_months(first_of_month(approved_on), 1)), app_row["instalments"]
        remarks = f"Applied online (application #{app_row['id']})"
    if app_row["exceeds_policy"]:
        remarks += " — approved as exception to loan policy"
    conn.execute("""INSERT INTO advances_loans (loan_ref, emp_id, loan_type, date_given, amount, monthly_instalment, first_recovery_month,
                                               purpose, no_of_instalments, recommended_by, approved_by, approval_date, remarks, disbursed,
                                               repayment_method, lump_sum_due)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                 (ref, app_row["emp_id"], app_row["loan_type"], fmt_date(approved_on), amount, instalment, first_recovery,
                  app_row["purpose"], instalments, app_row["reviewed_by"], user, fmt_date(approved_on), remarks,
                  LUMP_SUM if lump else INSTALMENTS, app_row["lump_sum_date"] if lump else None))
    return ref, instalment

def record_repayment(conn, loan_ref, amount_text, paid_on_text, note, user, now):
    """Record money a staff member paid back directly. Returns (loan row, amount, balance left)."""
    ln = conn.execute("SELECT a.*, s.employee_name FROM advances_loans a JOIN staff s ON s.emp_id = a.emp_id WHERE a.loan_ref = ?",
                      ((loan_ref or "").strip(),)).fetchone()
    paid_on = parse_date(paid_on_text)
    amount = round(checked_number(amount_text, "repayment_invalid"), 0)
    if ln is None or paid_on is None or paid_on > date.today() or fmt_date(paid_on) < (ln["date_given"] or "") or amount <= 0:
        raise ChangeError("repayment_invalid")
    balance = loan_balance(ln, first_of_month(date.today()), loan_repaid(conn, ln["loan_ref"]))
    if amount > balance + 0.5:
        raise ChangeError("repayment_too_much")
    conn.execute("INSERT INTO loan_repayments (loan_ref, emp_id, amount, paid_on, note, recorded_by, recorded_at) VALUES (?,?,?,?,?,?,?)",
                 (ln["loan_ref"], ln["emp_id"], amount, fmt_date(paid_on), (note or "").strip()[:200], user, now))
    return ln, amount, max(0.0, balance - amount)
