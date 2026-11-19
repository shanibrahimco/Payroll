"""Timesheets and their codes."""

from common.constants import ARTICLE_TYPES, EOBI_ONLY_TYPE, TS_CODE_FIELDS, TS_TEXT_FIELDS
from common.dates import fmt_date, parse_date, pay_month_label
from common.errors import ChangeError
from common.settings import log_decision
from timesheets.rules import decide_timesheet, get_timesheet, save_timesheet

def handle(self, conn, path, data, ym, pm_str, user, role, own_emp, cfg, cutoff, km_rate, now, enters_for_others):
    if path == "/save_timesheet":
        emp_id = data.get("emp_id", "").strip().upper() if enters_for_others else own_emp
        if not emp_id:
            raise ChangeError("no_emp_link")
        return save_timesheet(conn, emp_id, pm_str, data, self.files, data.get("action") == "submit", user, cfg)

    if path == "/set_ts_access":
        emp_id = data.get("emp_id", "").strip().upper()
        staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
        if (staff is None or staff["status"] != "Active" or staff["staff_type"] in ARTICLE_TYPES + (EOBI_ONLY_TYPE,)):
            raise ChangeError("ts_access_invalid")
        if data.get("enable") == "1":
            start = parse_date(f"{data.get('from_month', '')}-01")
            if not start:
                raise ChangeError("ts_access_invalid")
            conn.execute("UPDATE staff SET timesheet_from = ? WHERE emp_id = ?", (fmt_date(start), emp_id))
            summary = f"{emp_id} {staff['employee_name']}: timesheets required from {pay_month_label(fmt_date(start))} (salary held until approved)"
            result = "ts_access_on"
        else:
            conn.execute("UPDATE staff SET timesheet_from = NULL WHERE emp_id = ?", (emp_id,))
            summary = f"{emp_id} {staff['employee_name']}: timesheets no longer required"
            result = "ts_access_off"
        log_decision(conn, "ts_access", emp_id, None, {"timesheet_from": data.get("from_month") if result == "ts_access_on" else None}, summary, user)
        return result

    if path == "/save_ts_code":
        category, code = data.get("category"), data.get("code", "").strip().upper()
        description = data.get("description", "").strip()[:120]
        if category not in TS_CODE_FIELDS or not code or len(code) > 12 or not all(c.isalnum() or c in "-/" for c in code):
            raise ChangeError("code_invalid")
        old = conn.execute("SELECT description FROM timesheet_codes WHERE category = ? AND code = ?", (category, code)).fetchone()
        conn.execute("""INSERT INTO timesheet_codes (category, code, description, added_by, added_at) VALUES (?,?,?,?,?)
                            ON CONFLICT(category, code) DO UPDATE SET description = excluded.description""",
                     (category, code, description, user, now))
        change = (f"description '{old['description'] or ''}' → '{description}'" if old else f"added{': ' + description if description else ''}")
        log_decision(conn, "ts_code", None, None, {"category": category, "code": code, "description": description},
                     f"{TS_TEXT_FIELDS[category]} {code} {change}", user)
        return "code_saved"

    if path == "/decide_timesheet":
        ts = get_timesheet(conn, data.get("emp_id", ""), pm_str)
        if ts is None:
            raise ChangeError("bad_input")
        return decide_timesheet(conn, ts, data.get("decision"), data.get("note", ""), user, role, own_emp)
