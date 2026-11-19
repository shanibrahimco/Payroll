"""Change requests: new staff, pay changes, adjustments and their approval."""

from datetime import datetime
import json

import config
from auth.roles import ROLE_APPROVER
from common.constants import ARTICLE_TYPES, EOBI_ONLY_TYPE, NOC_TYPE
from common.dates import add_months, first_of_month, fmt_date, parse_date, pay_month_label
from common.errors import ChangeError
from common.numbers import checked_number
from common.settings import is_month_locked
from loans.rules import loan_policy
from staff.documents import delete_request_documents

KIND_LABELS = {"add_staff": "New staff member", "update_staff": "Staff / pay change",
               "add_input": "Monthly adjustment", "add_loan": "Advance / loan",
               "travel_rate": "Travel rate per km", "salary_release": "Withheld salary released", "late_hold_release": "Late-arrival hold released", "staff_timing": "Staff office timing", "part_time_hours": "Part-timer weekly hours",
               "ts_code": "Timesheet code (working legend)", "ts_access": "Timesheets required (non-ICAP staff)",
               "staff_email": "Staff email addresses", "portal_delegate": "Portal operated for staff",
               "staff_document": "Staff document", "doc_type": "Required documents", "payroll_import": "Final payroll sheet",
               "attendance": "Attendance", "work_location": "Mobile attendance setup", "leave_import": "Leave history upload", "security": "Held pay / security settled", "holiday": "Gazetted holiday", "leave_credit": "Leave credit (holiday / weekend worked)"}

STAFF_EDITABLE = {  # column -> label
    "staff_type": "Staff Type", "date_of_joining": "Date of Joining", "monthly_salary": "Monthly Salary",
    "weekly_hours": "Weekly Hours", "icap_reg_confirmed_date": "ICAP Registration Date",
    "eobi_registration_date": "EOBI Registration Date", "status": "Status", "leaving_date": "Leaving Date",
    "eobi_no": "EOBI No.", "date_of_birth": "Date of Birth", "icap_crn": "ICAP CRN", "training_org": "ICAP Training Organisation",
    "cnic_issue_date": "CNIC Issue Date", "cnic_expiry": "CNIC Expiry",
    "leave_entitlement_override": "Residual Leaves (NOC)", "articleship_end_date": "Residual Period Ends (NOC)",
}

NEW_STAFF_LABELS = {  # column -> label, for the add-staff form
    "employee_name": "Name", "cnic": "CNIC", "designation": "Designation", "staff_type": "Staff Type",
    "date_of_joining": "Date of Joining", "monthly_salary": "Monthly Salary", "weekly_hours": "Weekly Hours",
    "bank_name": "Bank", "bank_account_iban": "IBAN", "account_title": "Account Title",
    "eobi_no": "EOBI No.", "date_of_birth": "Date of Birth", "eobi_registration_date": "EOBI Registration Date",
    "icap_crn": "ICAP CRN", "cnic_issue_date": "CNIC Issue Date", "cnic_expiry": "CNIC Expiry",
    "icap_reg_confirmed_date": "ICAP Card Received", "training_org": "ICAP Training Organisation",
    "leave_entitlement_override": "Residual Leaves (NOC)", "articleship_end_date": "Residual Period Ends (NOC)",
}

INPUT_LABELS = {"bonus": "Bonus", "arrears_other": "Arrears",
                "partial_days_client_deduction": "Client Ded.", "leave_days_waived": "Waived Leaves"}

def fmt_change_value(col, val):
    if val is None or val == "":
        return "(blank)"
    if col == "monthly_salary":
        return f"Rs. {float(val):,.0f}"
    if col == "weekly_hours":
        return f"{float(val):g} h"
    return str(val)

def training_org_of(staff):
    """The firm ICAP records this student's training under (not necessarily the firm that pays them)."""
    return staff["training_org"] or config.TRAINING_ORGS[0]

def normalise_hours(value, cfg):
    hours = checked_number(value, "hours_invalid")
    if hours < 0:
        raise ChangeError("hours_invalid")
    return None if hours >= float(cfg.get("standard_weekly_hours", 45)) else hours

def build_change(conn, kind, data, pm_str, cfg):
    """Validate a submitted form; returns (emp_id, payroll_month, payload, summary)."""
    emp_id = data.get("emp_id", "").strip().upper()
    staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()

    if kind == "add_staff":
        eobi_only = data.get("staff_type") == EOBI_ONLY_TYPE
        # Employees need a joining date and salary; EOBI-only people need just their EOBI registration date
        if (not emp_id.isalnum() or not data.get("employee_name", "").strip()
                or (not eobi_only and (not data.get("monthly_salary") or not data.get("date_of_joining")))
                or (eobi_only and not data.get("eobi_registration_date"))):
            raise ChangeError("bad_input")
        if staff:
            raise ChangeError("emp_exists")
        fields = {"emp_id": emp_id, "status": "Active"}
        for col in NEW_STAFF_LABELS:
            val = data.get(col, "").strip()
            if val:
                fields[col] = val
        fields["monthly_salary"] = 0.0 if eobi_only else checked_number(fields["monthly_salary"])
        if fields["monthly_salary"] < 0:
            raise ChangeError("bad_input")
        if data.get("staff_type") in ARTICLE_TYPES:
            fields["training_org"] = fields.get("training_org", config.TRAINING_ORGS[0])
            if fields["training_org"] not in config.TRAINING_ORGS:
                raise ChangeError("bad_input")
        else:
            fields.pop("training_org", None)
        if data.get("staff_type") == NOC_TYPE:  # a transferred articleship: only what remains of the leaves and the period
            if not parse_date(fields.get("articleship_end_date")) or "leave_entitlement_override" not in fields:
                raise ChangeError("noc_needs_residual")
            fields["leave_entitlement_override"] = checked_number(fields["leave_entitlement_override"])
        else:
            fields.pop("leave_entitlement_override", None)
            fields.pop("articleship_end_date", None)
        if "weekly_hours" in fields:
            fields["weekly_hours"] = normalise_hours(fields["weekly_hours"], cfg)
        summary = f"{emp_id} — " + "; ".join(f"{NEW_STAFF_LABELS[c]}: {fmt_change_value(c, v)}"
                                             for c, v in fields.items() if c in NEW_STAFF_LABELS)
        return emp_id, None, {"fields": fields}, summary

    if not staff:
        raise ChangeError("emp_missing")
    name = staff["employee_name"]

    if kind == "update_staff":
        changes = {}
        for col in STAFF_EDITABLE:
            val = data.get(col, "").strip()
            if not val:
                continue
            if col == "training_org":
                if val not in config.TRAINING_ORGS:
                    raise ChangeError("bad_input")
                if (data.get("staff_type") or staff["staff_type"]) not in ARTICLE_TYPES:
                    continue  # only ICAP and NOC students are registered with a training organisation
            if col in ("monthly_salary", "leave_entitlement_override"):
                val = checked_number(val)
                if val < 0:
                    raise ChangeError("bad_input")
            elif col == "weekly_hours":
                val = normalise_hours(val, cfg)
            if val != staff[col]:
                changes[col] = val
        if not changes:
            raise ChangeError("nothing_changed")
        summary = f"{emp_id} {name}: " + "; ".join(
            f"{STAFF_EDITABLE[c]} {fmt_change_value(c, staff[c])} → {fmt_change_value(c, v)}" for c, v in changes.items())
        return emp_id, None, {"emp_id": emp_id, "changes": changes}, summary

    if kind == "add_input":
        if is_month_locked(conn, pm_str):
            raise ChangeError("locked")
        values = {c: checked_number(data.get(c)) for c in INPUT_LABELS}
        existing = conn.execute("SELECT 1 FROM monthly_inputs WHERE payroll_month = ? AND emp_id = ?", (pm_str, emp_id)).fetchone()
        parts = [f"{INPUT_LABELS[c]} {v:g}" if c == "leave_days_waived" else f"{INPUT_LABELS[c]} Rs. {v:,.0f}"
                 for c, v in values.items() if v]
        summary = (f"{emp_id} {name} — {pay_month_label(pm_str)}: " + ("; ".join(parts) or "all amounts zero")
                   + (" (replaces existing entry)" if existing else ""))
        return emp_id, pm_str, {"emp_id": emp_id, "payroll_month": pm_str, "values": values, "note": data.get("note", "")}, summary

    if kind == "add_loan":
        ref = data.get("loan_ref", "").strip().upper()
        given = parse_date(data.get("date_given"))
        if not ref or not given or not data.get("amount"):
            raise ChangeError("bad_input")
        loan_type = data.get("loan_type") if data.get("loan_type") in ("Loan", "Advance") else "Loan"
        amount = checked_number(data["amount"])
        instalment = checked_number(data.get("monthly_instalment"))
        if amount <= 0 or instalment < 0:
            raise ChangeError("bad_input")
        first_m = fmt_date(add_months(first_of_month(given), 1))
        existing = conn.execute("SELECT 1 FROM advances_loans WHERE loan_ref = ?", (ref,)).fetchone()
        if loan_type == "Loan" and (instalment <= 0 or -(-amount // instalment) > int(float(cfg.get("loan_max_instalments", 20)))):
            raise ChangeError("loan_instalments")
        limit, outstanding, exceeds = loan_policy(conn, staff, 0 if existing else amount, first_m, cfg)
        recovery = f"Rs. {instalment:,.0f}/month" if loan_type == "Loan" else "recovered in full"
        summary = (f"{emp_id} {name} — {loan_type} {ref}: Rs. {amount:,.0f}, {recovery} from {pay_month_label(first_m)}"
                   + (" (replaces existing loan)" if existing else "")
                   + (f" — EXCEEDS POLICY: outstanding would be Rs. {outstanding + amount:,.0f} against the limit of Rs. {limit:,.0f}" if exceeds else ""))
        payload = {"loan_ref": ref, "emp_id": emp_id, "loan_type": loan_type, "date_given": fmt_date(given),
                   "amount": amount, "monthly_instalment": instalment, "first_recovery_month": first_m,
                   "purpose": data.get("purpose", "")}
        return emp_id, first_m, payload, summary

    raise ChangeError("bad_input")

def apply_change(conn, kind, payload):
    if kind == "add_staff":
        fields = {c: v for c, v in payload["fields"].items() if c in NEW_STAFF_LABELS or c in ("emp_id", "status")}
        if conn.execute("SELECT 1 FROM staff WHERE emp_id = ?", (fields["emp_id"],)).fetchone():
            raise ChangeError("emp_exists")
        conn.execute(f"INSERT INTO staff ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})", list(fields.values()))
    elif kind == "update_staff":
        for col, val in payload["changes"].items():
            if col in STAFF_EDITABLE:  # column names come only from this whitelist
                conn.execute(f"UPDATE staff SET {col} = ? WHERE emp_id = ?", (val, payload["emp_id"]))
    elif kind == "add_input":
        if is_month_locked(conn, payload["payroll_month"]):
            raise ChangeError("locked")
        v = payload["values"]
        conn.execute("""
            INSERT OR REPLACE INTO monthly_inputs (payroll_month, emp_id, bonus, arrears_other, late_arrival_deduction, partial_days_client_deduction, leave_days_waived, note)
            VALUES (?,?,?,?,?,?,?,?)
        """, (payload["payroll_month"], payload["emp_id"], v["bonus"], v["arrears_other"], 0.0,
              v["partial_days_client_deduction"], v["leave_days_waived"], payload["note"]))
    elif kind == "add_loan":
        p = payload
        conn.execute("""
            INSERT OR REPLACE INTO advances_loans (loan_ref, emp_id, loan_type, date_given, amount, monthly_instalment, first_recovery_month, purpose)
            VALUES (?,?,?,?,?,?,?,?)
        """, (p["loan_ref"], p["emp_id"], p["loan_type"], p["date_given"], p["amount"], p["monthly_instalment"],
              p["first_recovery_month"], p["purpose"]))

def submit_change(conn, kind, data, pm_str, cfg, user, role):
    """Approver: apply now. Preparer: queue for approval. Returns a FLASH_MESSAGES key."""
    emp_id, month, payload, summary = build_change(conn, kind, data, pm_str, cfg)
    now = datetime.now().isoformat(timespec="seconds")
    approved = role == ROLE_APPROVER
    if approved:
        apply_change(conn, kind, payload)
    conn.execute("""
        INSERT INTO change_requests (kind, emp_id, payroll_month, payload, summary, requested_by, requested_at, status, decided_by, decided_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (kind, emp_id, month, json.dumps(payload), summary, user, now,
          "Approved" if approved else "Pending", user if approved else None, now if approved else None))
    return "applied" if approved else "submitted"

def decide_change(conn, request_id, approve, user):
    req = conn.execute("SELECT * FROM change_requests WHERE id = ? AND status = 'Pending'", (request_id,)).fetchone()
    if req is None:
        raise ChangeError("bad_input")
    if approve:
        apply_change(conn, req["kind"], json.loads(req["payload"]))
    elif req["kind"] == "add_staff":
        delete_request_documents(conn, request_id)  # the person is not added, so their documents aren't kept
    conn.execute("UPDATE change_requests SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                 ("Approved" if approve else "Rejected", user, datetime.now().isoformat(timespec="seconds"), request_id))
    return "req_approved" if approve else "req_rejected"
