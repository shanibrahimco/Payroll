"""Loan applications and loan bank files."""

import json

import config
from auth.roles import ROLE_REVIEWER
from auth.sessions import approver_only_emps
from common.constants import EOBI_ONLY_TYPE
from common.errors import ChangeError
from common.numbers import checked_number
from common.settings import log_decision
from loans.rules import LUMP_SUM, approve_loan_application, loan_policy, lump_sum_date, record_repayment
from notifications.mailer import notify_approver
from payroll.bank_files import generate_loan_bank_files

def handle(self, conn, path, data, ym, pm_str, user, role, own_emp, cfg, cutoff, km_rate, now, enters_for_others):
    if path == "/apply_loan":
        if not own_emp:
            raise ChangeError("no_emp_link")
        staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (own_emp,)).fetchone()
        if staff is None or staff["status"] != "Active" or staff["staff_type"] == EOBI_ONLY_TYPE:
            raise ChangeError("not_employee")
        loan_type = "Advance" if data.get("loan_type") == "Advance" else "Loan"
        lump = loan_type == "Loan" and data.get("repayment") == LUMP_SUM  # an advance is always recovered from next month's salary
        amount = round(checked_number(data.get("amount"), "loan_invalid"), 0)
        instalments = 1 if loan_type == "Advance" or lump else int(checked_number(data.get("instalments"), "loan_invalid"))
        due = lump_sum_date(data.get("lump_sum_date"), cfg) if lump else None
        purpose = data.get("purpose", "").strip()[:200]
        if amount <= 0 or not purpose or instalments < 1:
            raise ChangeError("loan_invalid")
        if instalments > int(float(cfg.get("loan_max_instalments", 20))):
            raise ChangeError("loan_instalments")
        limit, outstanding, exceeds = loan_policy(conn, staff, amount, pm_str, cfg)
        conn.execute("""INSERT INTO loan_applications (emp_id, loan_type, amount, instalments, purpose, applied_by, applied_at,
                                                           policy_limit, outstanding, exceeds_policy, repayment_method, lump_sum_date)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (own_emp, loan_type, amount, instalments, purpose, user, now, limit, outstanding, 1 if exceeds else 0,
                      LUMP_SUM if lump else "Instalments", due.isoformat() if due else None))
        return "loan_applied"

    if path == "/record_loan_repayment":
        ln, amount, left = record_repayment(conn, data.get("loan_ref"), data.get("amount"), data.get("paid_on"), data.get("note"), user, now)
        log_decision(conn, "add_loan", ln["emp_id"], None, {"loan_ref": ln["loan_ref"], "repaid": amount, "paid_on": data.get("paid_on")},
                     f"{ln['emp_id']} {ln['employee_name']}: Rs. {amount:,.0f} of loan {ln['loan_ref']} repaid directly on {data.get('paid_on')}"
                     + (" — loan fully repaid" if left <= 0 else f"; Rs. {left:,.0f} still to be repaid")
                     + (f" ({data.get('note', '').strip()[:200]})" if data.get("note", "").strip() else ""), user)
        return "repayment_saved"

    if path == "/export_loans":
        batch, ibft_path, ift_path, paid, skipped = generate_loan_bank_files(conn, user)
        if not paid:
            raise ChangeError("loan_files_none")
        result = {"batch": batch, "by": user, "at": now, "ibft": ibft_path, "ift": ift_path,
                  "paid": [f"{r['loan_ref']} {r['emp_id']} {r['employee_name']}: Rs. {r['amount']:,.0f}" for r in paid],
                  "skipped": [f"{r['loan_ref']} {r['emp_id']} {r['employee_name']}" for r in skipped]}
        conn.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('last_loan_batch', ?, 'Last loan bank-file batch')",
                     (json.dumps(result),))
        log_decision(conn, "add_loan", None, None, {"batch": batch, "loans": [r["loan_ref"] for r in paid]},
                     f"Loan payment batch {batch}: {len(paid)} loan(s), Rs. {sum(r['amount'] for r in paid):,.0f} by separate IBFT / IFT", user)
        return "loan_files_partly" if skipped else "loan_files_done"

    if path in ("/review_loan", "/decide_loan", "/withdraw_loan"):
        la = conn.execute("SELECT a.*, s.employee_name FROM loan_applications a JOIN staff s ON s.emp_id = a.emp_id WHERE a.id = ?",
                          (int(data["id"]),)).fetchone()
        if la is None:
            raise ChangeError("bad_input")
        decision = data.get("decision")
        note = data.get("note", "").strip()[:300]
        if path == "/withdraw_loan":
            if la["emp_id"] != own_emp or la["status"] != "Applied":
                raise ChangeError("loan_wrong_stage")
            conn.execute("UPDATE loan_applications SET status = 'Withdrawn' WHERE id = ?", (la["id"],))
            return "loan_withdrawn"
        approver_only = la["emp_id"] in approver_only_emps(conn)
        if (own_emp and la["emp_id"] == own_emp) or (role == ROLE_REVIEWER and approver_only):
            raise ChangeError("loan_own")
        repay = (f" repaid in one lump sum by {la['lump_sum_date']}" if la["repayment_method"] == LUMP_SUM
                 else f" over {la['instalments']} instalments")
        desc = (f"{la['emp_id']} {la['employee_name']}: {la['loan_type']} Rs. {la['amount']:,.0f}"
                + (repay if la["loan_type"] == "Loan" else "") + f" — {la['purpose']}")
        if path == "/review_loan":
            if la["status"] != "Applied" or decision not in ("Recommended", "Rejected"):
                raise ChangeError("loan_wrong_stage")
            if decision == "Recommended" and la["exceeds_policy"] and not (data.get("exception") == "1" and note):
                raise ChangeError("loan_need_exception")
            if decision == "Rejected" and not note:
                raise ChangeError("claim_need_note")
            conn.execute("UPDATE loan_applications SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ? WHERE id = ?",
                         (decision, user, now, note, la["id"]))
            if decision == "Recommended":
                notify_approver(conn, user, f"{config.FIRM_SHORT} payroll — loan application awaiting your approval",
                                f"{user} recommended a loan application:\n\n  {desc}\n"
                                + (f"  EXCEEDS POLICY (limit Rs. {la['policy_limit']:,.0f}) — exception: {note}\n" if la["exceeds_policy"] else "")
                                + f"\nPlease approve or reject it on the Approvals tab: {cfg.get('app_url', '')}\n")
                return "loan_recommended"
            return "loan_rejected"
        # Approver: a recommended application, or one the reviewer may not review (his own / a portal he operates)
        if not (la["status"] == "Recommended" or (la["status"] == "Applied" and approver_only)) or decision not in ("Approved", "Rejected"):
            raise ChangeError("loan_wrong_stage")
        if decision == "Rejected":
            conn.execute("UPDATE loan_applications SET status = 'Rejected', decided_by = ?, decided_at = ?, decision_note = ? WHERE id = ?",
                         (user, now, note, la["id"]))
            log_decision(conn, "add_loan", la["emp_id"], None, {"application": la["id"]}, f"Application #{la['id']} rejected — {desc}", user)
            return "loan_rejected"
        amount = round(checked_number(data.get("approved_amount"), "loan_bad_amount", la["amount"]), 0)
        if not 0 < amount <= la["amount"]:
            raise ChangeError("loan_bad_amount")
        ref, instalment = approve_loan_application(conn, la, amount, user, now)
        conn.execute("""UPDATE loan_applications SET status = 'Approved', approved_amount = ?, decided_by = ?, decided_at = ?, decision_note = ?,
                            loan_ref = ? WHERE id = ?""", (amount, user, now, note, ref, la["id"]))
        log_decision(conn, "add_loan", la["emp_id"], None, {"application": la["id"], "loan_ref": ref, "amount": amount},
                     f"Application #{la['id']} approved as {ref}: Rs. {amount:,.0f}"
                     + ((f", lump sum due {la['lump_sum_date']}" if la["repayment_method"] == LUMP_SUM else f", Rs. {instalment:,.0f}/month")
                        if la["loan_type"] == "Loan" else ", recovered in full next month")
                     + (" (EXCEPTION to loan policy)" if la["exceeds_policy"] else "") + f" — {desc}", user)
        return "loan_approved"
