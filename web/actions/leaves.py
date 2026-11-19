"""Leave, credit leave and gazetted holidays."""

from datetime import date
import json
import os

import config
from auth.roles import ROLE_APPROVER, ROLE_REVIEWER
from auth.sessions import approver_only_emps
from common.constants import ABSENCE_LEAVE_TYPE, ARTICLE_TYPES, CREDIT_REASONS, EOBI_ONLY_TYPE, LEAVE_TYPES, MAX_UPLOAD_BYTES
from common.dates import fmt_date, get_payroll_month_for_event, parse_date
from common.errors import ChangeError
from common.settings import log_decision
from leaves.history_import import apply_leave_history, read_leave_history
from leaves.rules import check_credit, check_entry, clean_leave_entry, credit_is_late, decide_leave
from notifications.mailer import email_holiday_notice, notify_approver

def handle(self, conn, path, data, ym, pm_str, user, role, own_emp, cfg, cutoff, km_rate, now, enters_for_others):
    if path == "/leave_import_upload":
        upload = self.files.get("leave_file")
        if not upload or not upload[2] or not upload[2].startswith(b"PK\x03\x04") or len(upload[2]) > MAX_UPLOAD_BYTES:
            raise ChangeError("leave_file_unreadable")
        try:
            parsed = read_leave_history(conn, upload[2])
        except ChangeError:
            raise
        except Exception:
            raise ChangeError("leave_file_unreadable")
        conn.execute("UPDATE leave_imports SET status = 'Discarded' WHERE status = 'Preview'")
        conn.execute("INSERT INTO leave_imports (file_name, uploaded_by, uploaded_at, data) VALUES (?,?,?,?)",
                     (os.path.basename(upload[0].replace("\\", "/"))[:120], user, now, json.dumps(parsed)))
        return "leave_file_previewed"

    if path in ("/leave_import_apply", "/leave_import_discard"):
        imp = conn.execute("SELECT * FROM leave_imports WHERE id = ? AND status = 'Preview'", (int(data["id"]),)).fetchone()
        if imp is None:
            raise ChangeError("bad_input")
        if path == "/leave_import_discard":
            conn.execute("UPDATE leave_imports SET status = 'Discarded' WHERE id = ?", (imp["id"],))
            return "sheet_discarded"
        parsed = json.loads(imp["data"])
        added, skipped = apply_leave_history(conn, parsed, user, data.get("replace") == "1")
        conn.execute("UPDATE leave_imports SET status = 'Applied', applied_at = ? WHERE id = ?", (now, imp["id"]))
        log_decision(conn, "leave_import", None, None, {"import": imp["id"]},
                     f"Leave history '{imp['file_name']}' applied: opening balances (as at 31 Dec 2025) for {len(parsed['opening'])} staff; "
                     f"{added} leave record(s) added as approved, {skipped} already on file"
                     + ("; earlier 2026 records of those staff replaced" if data.get("replace") == "1" else ""), user)
        return "leave_file_applied"

    if path in ("/add_holiday", "/delete_holiday"):
        d = parse_date(data.get("holiday_date"))
        name = data.get("name", "").strip()[:80]
        if d is None or (path == "/add_holiday" and not name):
            raise ChangeError("holiday_invalid")
        if path == "/add_holiday":
            conn.execute("INSERT OR REPLACE INTO holidays (holiday_date, name, added_by, added_at) VALUES (?,?,?,?)", (fmt_date(d), name, user, now))
            summary = f"{d.strftime('%a %d %b %Y')}: {name}"
        else:
            conn.execute("DELETE FROM holidays WHERE holiday_date = ?", (fmt_date(d),))
            summary = f"{d.strftime('%a %d %b %Y')} removed"
        log_decision(conn, "holiday", None, None, {"date": fmt_date(d), "name": name}, summary, user)
        if d < date.today():  # a past date entered for the record: nothing to announce
            return "holiday_saved" if path == "/add_holiday" else "holiday_deleted"
        conn.commit()  # the holiday stands whatever happens to the email
        when = d.strftime("%a %d %b %Y")
        headline = f"A gazetted holiday has been announced: {when}, {name}." if path == "/add_holiday" else f"The gazetted holiday on {when} has been cancelled."
        return {"sent": "holiday_notice_sent", "partly": "holiday_notice_partly", "failed": "holiday_notice_failed",
                "no_account": "holiday_notice_no_account", "no_staff": "holiday_notice_no_staff"}[email_holiday_notice(conn, user, now, headline)]

    if path == "/email_holidays":
        outcome = email_holiday_notice(conn, user, now)
        if outcome == "no_account":
            raise ChangeError("mail_no_account")
        if outcome in ("no_staff", "nothing"):
            raise ChangeError("holidays_none")
        return {"sent": "holidays_emailed", "failed": "mail_failed", "partly": "holidays_email_partly"}[outcome]

    if path == "/apply_credit":
        if not own_emp:
            raise ChangeError("no_emp_link")
        staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (own_emp,)).fetchone()
        if staff is None or staff["status"] != "Active" or staff["staff_type"] == EOBI_ONLY_TYPE:
            raise ChangeError("not_employee")
        if data.get("full_day") != "1":
            raise ChangeError("credit_full_day")
        note = data.get("note", "").strip()[:200]
        if not note:
            raise ChangeError("credit_need_note")
        d = parse_date(data.get("work_date"))
        if d is None:
            raise ChangeError("credit_date")
        holiday = conn.execute("SELECT 1 FROM holidays WHERE holiday_date = ?", (fmt_date(d),)).fetchone()
        reason = "weekend_called" if not holiday else "holiday_client" if staff["staff_type"] in ARTICLE_TYPES else "holiday_called"
        work_date = check_credit(conn, staff, data.get("work_date"), reason)
        late = credit_is_late(work_date, cfg)
        conn.execute("""INSERT INTO leave_credits (emp_id, work_date, days, reason, note, source, status, requested_by, requested_at, applied_late)
                            VALUES (?,?,1,?,?,'Application',?,?,?,?)""", (own_emp, work_date, reason, note, "Late" if late else "Applied", user, now, 1 if late else 0))
        if own_emp in approver_only_emps(conn):
            notify_approver(conn, user, f"{config.FIRM_SHORT} payroll — credit leave application awaiting your decision",
                            f"{own_emp} {staff['employee_name']} applied for one credit leave for {work_date}: {note}\n\n"
                            f"Please decide it on the Approvals tab: {cfg.get('app_url', '')}\n")
        return "credit_submitted_late" if late else "credit_submitted"

    if path == "/review_credit":
        credit = conn.execute("SELECT c.*, s.employee_name FROM leave_credits c JOIN staff s ON s.emp_id = c.emp_id WHERE c.id = ?",
                              (int(data["id"]),)).fetchone()
        if credit is None or credit["status"] not in ("Applied", "Late"):
            raise ChangeError("credit_wrong_stage")
        decision, note = data.get("decision"), data.get("note", "").strip()[:200]
        only_approver = credit["emp_id"] in approver_only_emps(conn)
        if role == ROLE_REVIEWER and only_approver:
            raise ChangeError("credit_own")
        if role == ROLE_APPROVER and not only_approver:
            raise ChangeError("credit_wrong_stage")  # the reviewer recommends or condones it first
        if decision not in ("Recommended", "Rejected"):
            raise ChangeError("bad_input")
        late = credit["status"] == "Late"
        if (late or decision == "Rejected") and not note:
            raise ChangeError("credit_need_note")
        desc = f"{credit['emp_id']} {credit['employee_name']}: credit leave for {credit['work_date']}"
        if decision == "Rejected":
            conn.execute("UPDATE leave_credits SET status = 'Rejected', decided_by = ?, decided_at = ?, condone_note = ? WHERE id = ?", (user, now, note, credit["id"]))
            log_decision(conn, "leave_credit", credit["emp_id"], None, {"id": credit["id"], "decision": "Rejected"}, f"{desc} rejected — {note}", user)
            return "credit_rejected"
        if late:
            conn.execute("UPDATE leave_credits SET condoned_by = ?, condoned_at = ?, condone_note = ? WHERE id = ?", (user, now, note, credit["id"]))
        if role == ROLE_APPROVER:
            conn.execute("UPDATE leave_credits SET status = 'Approved', decided_by = ?, decided_at = ? WHERE id = ?", (user, now, credit["id"]))
            log_decision(conn, "leave_credit", credit["emp_id"], None, {"id": credit["id"], "decision": "Approved"},
                         f"{desc} approved" + (f"; delay condoned — {note}" if late else ""), user)
            return "credit_approved"
        conn.execute("UPDATE leave_credits SET status = 'Recommended', recommended_by = ?, recommended_at = ? WHERE id = ?", (user, now, credit["id"]))
        log_decision(conn, "leave_credit", credit["emp_id"], None, {"id": credit["id"], "decision": "Recommended", "condoned": late},
                     f"{desc} recommended" + (f"; delay condoned — {note}" if late else ""), user)
        notify_approver(conn, user, f"{config.FIRM_SHORT} payroll — leave credit awaiting your approval",
                        f"{user} recommended a credit leave:\n\n  {desc}\n" + (f"  Delay condoned: {note}\n" if late else "") +
                        f"\nPlease approve or reject it on the Approvals tab: {cfg.get('app_url', '')}\n")
        return "credit_condoned" if late else "credit_recommended"

    if path == "/add_credit":
        emp_id = data.get("emp_id", "").strip().upper()
        staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
        if staff is None or staff["status"] != "Active" or staff["staff_type"] == EOBI_ONLY_TYPE:
            raise ChangeError("emp_missing")
        if role == ROLE_REVIEWER and emp_id == own_emp:
            raise ChangeError("credit_own")
        reason = data.get("reason", "")
        work_date = check_credit(conn, staff, data.get("work_date"), reason)
        days = 1.0  # credit leave is earned only for a complete working day
        approved = role == ROLE_APPROVER
        note = data.get("note", "").strip()[:200]
        late = credit_is_late(work_date, cfg)
        if late and not approved and not note:
            raise ChangeError("credit_need_note")  # entered after the limit: the reviewer records why the delay is condoned
        conn.execute("""INSERT INTO leave_credits (emp_id, work_date, days, reason, note, source, status, requested_by, requested_at, decided_by, decided_at,
                                                       applied_late, condoned_by, condoned_at, condone_note)
                            VALUES (?,?,?,?,?,'Reviewer',?,?,?,?,?,?,?,?,?)""",
                     (emp_id, work_date, days, reason, note, "Approved" if approved else "Recommended", user, now,
                      user if approved else None, now if approved else None,
                      1 if late else 0, user if late else None, now if late else None, note if late else None))
        summary = f"{emp_id} {staff['employee_name']}: +{days:g} leave for {parse_date(work_date).strftime('%a %d %b %Y')} — {CREDIT_REASONS[reason]}"
        if approved:
            log_decision(conn, "leave_credit", emp_id, None, {"work_date": work_date, "days": days, "reason": reason}, summary, user)
            return "credit_applied"
        notify_approver(conn, user, f"{config.FIRM_SHORT} payroll — leave credit awaiting your approval",
                        f"{user} recommended a leave credit:\n\n  {summary}\n{('  Note: ' + note) if note else ''}\n\n"
                        f"Please approve or reject it on the Approvals tab: {cfg.get('app_url', '')}\n")
        return "credit_added"

    if path == "/decide_credit":
        credit = conn.execute("SELECT c.*, s.employee_name FROM leave_credits c JOIN staff s ON s.emp_id = c.emp_id WHERE c.id = ?",
                              (int(data["id"]),)).fetchone()
        if credit is None or credit["status"] != "Recommended":
            raise ChangeError("credit_wrong_stage")
        decision = "Approved" if data.get("decision") == "Approved" else "Rejected"
        conn.execute("UPDATE leave_credits SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?", (decision, user, now, credit["id"]))
        log_decision(conn, "leave_credit", credit["emp_id"], None, {"id": credit["id"], "decision": decision},
                     f"{credit['emp_id']} {credit['employee_name']}: +{credit['days']:g} leave for {credit['work_date']} "
                     f"({CREDIT_REASONS.get(credit['reason'], credit['reason'])}) — {decision.lower()}", user)
        return "credit_approved" if decision == "Approved" else "credit_rejected"

    if path == "/decide_leave":
        leave = conn.execute("SELECT * FROM leave_register WHERE id = ?", (int(data["id"]),)).fetchone()
        if leave is None:
            raise ChangeError("bad_input")
        decide_leave(conn, leave, data.get("decision"), user, role, own_emp)
        return {"Recommended": "leave_recommended", "Approved": "leave_approved",
                "Rejected": "leave_rejected", "Cancelled": "leave_cancelled"}[data["decision"]]

    if path == "/bulk_leave":
        # Reviewer: recommend every Pending leave; Approver: approve every Recommended one. Skips what this user may not decide.
        decision = data.get("decision")
        if (decision, role) not in (("Recommended", ROLE_REVIEWER), ("Approved", ROLE_APPROVER)):
            raise ChangeError("not_allowed")
        for leave in conn.execute("SELECT * FROM leave_register WHERE status IN ('Pending', 'Recommended') ORDER BY id").fetchall():
            try:
                decide_leave(conn, leave, decision, user, role, own_emp)
            except ChangeError:
                continue
        return "leaves_bulk"

    if path == "/add_leave":
        emp_id = data["emp_id"].strip().upper() if enters_for_others else own_emp
        if not emp_id:
            raise ChangeError("no_emp_link")
        f_d, days = clean_leave_entry(data)
        pm_target = fmt_date(get_payroll_month_for_event(f_d, cutoff))
        check_entry(conn, emp_id, pm_target)
        if data["leave_type"] not in LEAVE_TYPES:
            raise ChangeError("bad_input")
        # Every leave starts Pending and goes through the chain; an absence is a record, not an application
        if data["leave_type"] == ABSENCE_LEAVE_TYPE:
            if not enters_for_others:
                raise ChangeError("no_absence_entry")
            status = "Recorded"
        else:
            status = "Pending"
        conn.execute("""
                INSERT INTO leave_register (application_date, emp_id, leave_type, date_from, date_to, no_of_days, reason, status, payroll_month, entered_by)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (data["application_date"], emp_id, data["leave_type"],
              data["date_from"], data["date_to"], days, data.get("reason",""),
              status, pm_target, user))
        return "saved"
