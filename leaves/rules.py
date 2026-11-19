"""Leave decisions and credit leave rules."""

from datetime import date

from attendance.timing import staff_timing_rows, timing_on
from auth.roles import ROLE_APPROVER, ROLE_REVIEWER
from auth.sessions import approver_only_emps
from common.constants import CREDIT_REASONS, EOBI_ONLY_TYPE
from common.dates import fmt_date, parse_date
from common.errors import ChangeError
from common.settings import get_settings_dict, is_month_locked

def decide_leave(conn, leave, decision, user, role, own_emp):
    """Leave chain: Pending -> Recommended (Reviewer) -> Approved (Approver); either may reject.
    Nobody decides their own leave, so a Reviewer's own leave (and that of staff whose portal he operates) goes straight
    to the Approver. A recorded absence isn't an application: it can only be cancelled if entered by mistake."""
    approver_only = leave["emp_id"] in approver_only_emps(conn)
    if (own_emp and leave["emp_id"] == own_emp) or (role == ROLE_REVIEWER and approver_only and leave["status"] != "Recorded"):
        raise ChangeError("own_leave")
    if is_month_locked(conn, leave["payroll_month"]):
        raise ChangeError("locked")
    status = leave["status"]
    applicant_is_reviewer = approver_only
    allowed = {
        "Recommended": role == ROLE_REVIEWER and status == "Pending",
        "Approved": role == ROLE_APPROVER and (status == "Recommended" or (status == "Pending" and applicant_is_reviewer)),
        "Rejected": status in ("Pending", "Recommended"),
        "Cancelled": status == "Recorded",
    }.get(decision, False)
    if not allowed:
        raise ChangeError("wrong_leave_stage")
    by_col = "recommended_by" if decision == "Recommended" else "approved_by"
    conn.execute(f"UPDATE leave_register SET status = ?, {by_col} = ? WHERE id = ?", (decision, user, leave["id"]))

def check_entry(conn, emp_id, payroll_month):
    """Leaves and travel need no pre-approval, but must be for existing staff and an open (unapproved) month."""
    staff = conn.execute("SELECT staff_type FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
    if not staff:
        raise ChangeError("emp_missing")
    if staff["staff_type"] == EOBI_ONLY_TYPE:
        raise ChangeError("not_employee")
    if is_month_locked(conn, payroll_month):
        raise ChangeError("locked")

def check_credit(conn, staff, work_date, reason):
    """Credits, for all staff including ICAP / NOC students: a gazetted holiday, or a Saturday / Sunday that is the person's day off.
    One credit per person per date."""
    if reason not in CREDIT_REASONS:
        raise ChangeError("credit_reason")
    d = parse_date(work_date)
    if d is None or d > date.today():
        raise ChangeError("credit_date")
    is_holiday = conn.execute("SELECT 1 FROM holidays WHERE holiday_date = ?", (fmt_date(d),)).fetchone() is not None
    timing = timing_on(staff_timing_rows(conn, staff["emp_id"]), fmt_date(d), get_settings_dict(conn))
    day_off = d.weekday() >= 5 and d.weekday() in timing["off"]  # a Saturday someone normally works earns no credit
    if (reason == "weekend_called" and not day_off) or (reason != "weekend_called" and not is_holiday):
        raise ChangeError("credit_date")
    if conn.execute("SELECT 1 FROM leave_credits WHERE emp_id = ? AND work_date = ? AND status != 'Rejected'",
                    (staff["emp_id"], fmt_date(d))).fetchone():
        raise ChangeError("credit_duplicate")
    return fmt_date(d)

def credit_is_late(work_date, cfg):
    return (date.today() - parse_date(work_date)).days > int(float(cfg.get("credit_apply_days", 7)))

def credit_review_form(c, ym, ok_label):
    """Recommend / condone / reject buttons for a credit leave application."""
    hint = "Reason for condoning or rejecting" if c["status"] == "Late" else "Reason (if rejecting)"
    return (f"<form method='POST' action='/review_credit?ym={ym}' style='display:flex; gap:6px; flex-wrap:wrap; align-items:center;'>"
            f"<input type='hidden' name='id' value='{c['id']}'><input name='note' size='22' placeholder='{hint}'>"
            f"<button class='btn btn-ok' type='submit' name='decision' value='Recommended'>{ok_label}</button>"
            "<button class='btn btn-danger' type='submit' name='decision' value='Rejected'>Reject</button></form>")
