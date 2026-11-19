"""Helpers for the mobile app API."""

from datetime import date
import math

from common.constants import STAFF_LEAVE_TYPES
from common.dates import fmt_date, parse_date
from common.errors import ChangeError
from loans.rules import loan_policy
from staff.rules import cnic_alert

MAX_JSON_BYTES = 256 * 1024

API_ERROR_STATUS = {"login_locked": 429, "unauthorized": 401, "login_invalid": 401, "not_allowed": 403, "pw_must_change": 403, "no_emp_link": 403,
                    "not_found": 404, "payslip_not_ready": 404, "server_error": 500}

API_WRITES = {"/api/leaves": "/add_leave", "/api/leaves/credit": "/apply_credit", "/api/travel": "/add_travel",
              "/api/travel/withdraw": "/withdraw_travel", "/api/loans": "/apply_loan", "/api/loans/withdraw": "/withdraw_loan"}

API_ROUTES = {("POST", p) for p in API_WRITES} | {("POST", p) for p in ("/api/login", "/api/logout", "/api/change_password",
                                                                        "/api/attendance/punch", "/api/leaves/withdraw")} \
             | {("GET", p) for p in ("/api/dashboard", "/api/attendance", "/api/leaves", "/api/travel", "/api/loans", "/api/payslip")}

def api_fields(data):
    """JSON values as the text form fields handle_change expects (true -> '1', as a ticked checkbox sends)."""
    return {k: "1" if v is True else "" if v is None or v is False else str(v) for k, v in data.items() if k != "emp_id"}

def api_check_numbers(fields, *names):
    for name in names:
        if fields.get(name, "") != "" and not math.isfinite(float(fields[name])):
            raise ChangeError("bad_input")

def api_clean_leave(fields):
    """Validate a leave application from the app and put its dates in the stored format."""
    d_from, d_to = parse_date(fields.get("date_from")), parse_date(fields.get("date_to"))
    applied = parse_date(fields.get("application_date")) if fields.get("application_date") else date.today()
    api_check_numbers(fields, "no_of_days")
    days = float(fields.get("no_of_days") or 0)
    if (d_from is None or d_to is None or applied is None or d_to < d_from or fields.get("leave_type") not in STAFF_LEAVE_TYPES
            or not 0 < days <= (d_to - d_from).days + 1):
        raise ChangeError("bad_input")
    fields.update(date_from=fmt_date(d_from), date_to=fmt_date(d_to), application_date=fmt_date(applied), reason=fields.get("reason", "")[:500])

def api_month(qs):
    """(YYYY-MM, YYYY-MM-01) from ?ym=, defaulting to the current month."""
    ym = qs.get("ym", [date.today().strftime("%Y-%m")])[0]
    if len(ym) != 7 or not parse_date(f"{ym}-01"):
        raise ChangeError("bad_input")
    return ym, f"{ym}-01"

def api_staff_row(conn, emp_id):
    staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
    if staff is None:
        raise ChangeError("emp_missing")
    return staff

def api_profile(staff):
    return {"emp_id": staff["emp_id"], "employee_name": staff["employee_name"], "designation": staff["designation"],
            "staff_type": staff["staff_type"], "cnic_expiry": staff["cnic_expiry"], "cnic_alert": cnic_alert(staff["cnic_expiry"])}

def api_today_punch(conn, emp_id):
    row = conn.execute("SELECT check_in_time, check_out_time, location_note, location_status FROM attendance WHERE emp_id = ? AND att_date = ?",
                       (emp_id, date.today().isoformat())).fetchone()
    check_in, check_out = (row["check_in_time"], row["check_out_time"]) if row else (None, None)
    return {"checked_in": bool(check_in), "check_in_time": check_in, "checked_out": bool(check_out), "check_out_time": check_out,
            "location_note": row["location_note"] if row else None, "location_status": row["location_status"] if row else None}

def api_loan_limits(conn, staff, pm_str, cfg):
    limit, outstanding, _ = loan_policy(conn, staff, 0, pm_str, cfg)
    return {"policy_limit": limit, "outstanding": outstanding, "available": max(0.0, limit - outstanding),
            "max_instalments": int(float(cfg.get("loan_max_instalments", 20)))}
