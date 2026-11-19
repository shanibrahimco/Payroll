"""Monthly payroll stages, sheet import and salary release."""

import json
import os
import secrets

import config
from auth.roles import ROLE_APPROVER
from common.constants import MAX_UPLOAD_BYTES
from common.dates import pay_month_label
from common.errors import ChangeError
from common.settings import is_month_locked, log_decision
from payroll.calculator import calculate_payroll
from payroll.sheet_import import apply_payroll_sheet, read_payroll_sheet

def handle(self, conn, path, data, ym, pm_str, user, role, own_emp, cfg, cutoff, km_rate, now, enters_for_others):
    if path == "/import_payroll_upload":
        upload = self.files.get("sheet")
        if not upload or not upload[2]:
            raise ChangeError("sheet_unreadable")
        name, content = os.path.basename(upload[0].replace("\\", "/"))[:120], upload[2]
        if len(content) > MAX_UPLOAD_BYTES or not (name.lower().endswith((".xlsx", ".xlsm", ".csv")) or content.startswith(b"PK\x03\x04")):
            raise ChangeError("sheet_unreadable")
        try:
            sheet_name, mapping, rows, unmatched = read_payroll_sheet(conn, name, content)
        except ChangeError:
            raise
        except Exception:
            raise ChangeError("sheet_unreadable")
        folder = os.path.join(config.UPLOAD_DIR, "payroll_sheets")
        os.makedirs(folder, exist_ok=True)
        stored = f"{pm_str[:7]}_{secrets.token_hex(6)}_{''.join(c for c in name if c.isalnum() or c in '._-')}"
        with open(os.path.join(folder, stored), "wb") as f:
            f.write(content)
        conn.execute("UPDATE payroll_imports SET status = 'Discarded' WHERE payroll_month = ? AND status = 'Preview'", (pm_str,))
        conn.execute("""INSERT INTO payroll_imports (payroll_month, file_name, stored_file, sheet_name, uploaded_by, uploaded_at, mapping, rows, unmatched)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                     (pm_str, name, stored, sheet_name, user, now, json.dumps(mapping), json.dumps(rows), json.dumps(unmatched)))
        return "sheet_previewed"

    if path in ("/import_payroll_apply", "/import_payroll_discard"):
        imp = conn.execute("SELECT * FROM payroll_imports WHERE id = ? AND status = 'Preview'", (int(data["id"]),)).fetchone()
        if imp is None:
            raise ChangeError("bad_input")
        if path == "/import_payroll_discard":
            conn.execute("UPDATE payroll_imports SET status = 'Discarded' WHERE id = ?", (imp["id"],))
            return "sheet_discarded"
        conn.commit()  # apply_payroll_sheet may recalculate the month on its own connection first
        apply_payroll_sheet(conn, imp, user, update_salaries=data.get("update_salaries") == "1")
        total = conn.execute("SELECT SUM(net_pay) FROM payroll_lines WHERE payroll_month = ?", (imp["payroll_month"],)).fetchone()[0] or 0
        log_decision(conn, "payroll_import", None, imp["payroll_month"], {"import": imp["id"], "file": imp["file_name"]},
                     f"{pay_month_label(imp['payroll_month'])} payroll replaced by the final sheet '{imp['file_name']}' "
                     f"({len(json.loads(imp['rows']))} staff, net pay Rs. {total:,.0f}); month approved and locked"
                     + (" — staff salaries updated from the sheet" if data.get("update_salaries") == "1" else ""), user)
        return "sheet_applied"

    if path in ("/prepare_month", "/review_month", "/approve_month", "/send_back_month"):
        conn.execute("INSERT OR IGNORE INTO payroll_runs (payroll_month, status) VALUES (?, 'Draft')", (pm_str,))
        stage = conn.execute("SELECT status FROM payroll_runs WHERE payroll_month = ?", (pm_str,)).fetchone()["status"]
        if path == "/prepare_month":
            if stage != "Draft":
                raise ChangeError("wrong_stage")
            conn.commit()
            calculate_payroll(pm_str)  # sign off the up-to-date figures
            conn.execute("UPDATE payroll_runs SET status = 'Prepared', prepared_by = ?, prepared_date = ? WHERE payroll_month = ?", (user, now, pm_str))
            return "month_prepared"
        if path == "/review_month":
            if stage != "Prepared":
                raise ChangeError("wrong_stage")
            conn.execute("UPDATE payroll_runs SET status = 'Reviewed', reviewed_by = ?, reviewed_date = ? WHERE payroll_month = ?", (user, now, pm_str))
            return "month_reviewed"
        if path == "/approve_month":
            if stage != "Reviewed":
                raise ChangeError("wrong_stage")
            conn.execute("UPDATE payroll_runs SET status = 'Approved', approved_by = ?, approved_date = ? WHERE payroll_month = ?", (user, now, pm_str))
            return "month_approved"
        if stage == "Draft" or (stage == "Approved" and role != ROLE_APPROVER):
            raise ChangeError("wrong_stage" if stage == "Draft" else "not_allowed")
        if conn.execute("SELECT imported_from FROM payroll_runs WHERE payroll_month = ?", (pm_str,)).fetchone()["imported_from"]:
            raise ChangeError("imported_month")  # recalculating would overwrite the final sheet's figures
        conn.execute("""UPDATE payroll_runs SET status = 'Draft', prepared_by = NULL, prepared_date = NULL, reviewed_by = NULL,
                            reviewed_date = NULL, approved_by = NULL, approved_date = NULL WHERE payroll_month = ?""", (pm_str,))
        return "month_sent_back"

    if path == "/release_salary":
        emp_id, withheld_month = data["emp_id"], data["withheld_month"]
        line = conn.execute("""SELECT p.salary_withheld_absence, p.employee_name, run.status FROM payroll_lines p
                                   JOIN payroll_runs run ON run.payroll_month = p.payroll_month
                                   WHERE p.emp_id = ? AND p.payroll_month = ?""", (emp_id, withheld_month)).fetchone()
        released = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM salary_releases WHERE emp_id = ? AND withheld_month = ?",
                                (emp_id, withheld_month)).fetchone()[0]
        if (line is None or line["status"] != "Approved" or withheld_month >= pm_str or is_month_locked(conn, pm_str)
                or line["salary_withheld_absence"] - released <= 0):
            raise ChangeError("release_invalid")
        amount = line["salary_withheld_absence"] - released
        conn.execute("INSERT INTO salary_releases (emp_id, withheld_month, amount, release_month, released_by, released_at) VALUES (?,?,?,?,?,?)",
                     (emp_id, withheld_month, amount, pm_str, user, now))
        log_decision(conn, "salary_release", emp_id, pm_str, {"withheld_month": withheld_month, "amount": amount},
                     f"{emp_id} {line['employee_name']}: Rs. {amount:,.0f} withheld in {pay_month_label(withheld_month)} "
                     f"released in {pay_month_label(pm_str)}", user)
        return "salary_released"
