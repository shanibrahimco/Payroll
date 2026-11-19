"""Leave history import (opening balances and past leaves)."""

from datetime import date
from datetime import datetime

import config
from attendance.records import name_key
from common.constants import (
    ABSENCE_LEAVE_TYPE,
    ARTICLE_TYPES,
    EOBI_ONLY_TYPE,
    LEAVE_TYPES,
    NOC_TYPE,
    STUDY_LEAVE_TYPE,
    URGENT_WORK_ABROAD,
    URGENT_WORK_PK,
)
from common.dates import fmt_date, get_payroll_month_for_event, parse_date
from common.errors import ChangeError
from common.settings import get_settings_dict
from payroll.sheet_import import _num

LEAVE_HISTORY_FROM = "2026-01-01"  # opening balances are the leaves taken before this date

def leave_type_from_text(text):
    t = (text or "").strip().lower()
    if "urgent" in t:
        if any(w in t for w in ("outside", "abroad", "foreign")):
            return URGENT_WORK_ABROAD
        if any(w in t for w in ("within", "inside", "pakistan")):
            return URGENT_WORK_PK
        return None  # say where: within or outside Pakistan
    for words, ltype in ((("study", "exam"), STUDY_LEAVE_TYPE), (("sick", "medical"), "Sick"), (("absen", "unapproved"), ABSENCE_LEAVE_TYPE)):
        if any(w in t for w in words):
            return ltype
    return None

def sheet_date(v):
    if isinstance(v, (datetime, date)):
        return v.date() if isinstance(v, datetime) else v
    s = str(v or "").strip()[:10]
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None

LEAVE_ROWS = 1500  # rows prepared on the Leaves sheet

def build_leave_template(conn):
    """Excel leave record: 'Opening Balance' (as at 1 Jan 2026, typed in by hand), 'Leaves' (one leave per row: employee
    and leave type from drop-downs, Emp ID and days filled in by formula), and a 'Summary' that keeps a running balance."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation
    head = (Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0F172A"))
    entry = PatternFill("solid", fgColor="FEF9C3")  # cells to fill in
    staff = conn.execute("""SELECT emp_id, employee_name, staff_type, leave_entitlement_override FROM staff
                            WHERE status = 'Active' AND COALESCE(staff_type, '') != ? ORDER BY emp_id""", (EOBI_ONLY_TYPE,)).fetchall()
    types = [t for t in LEAVE_TYPES if t != ABSENCE_LEAVE_TYPE]
    cfg = get_settings_dict(conn)
    wb = openpyxl.Workbook()

    ws = wb.active
    ws.title = "Opening Balance"
    ws.append([f"{config.FIRM_NAME} — Leave Record: opening balance as at 1 January 2026"])
    ws.append(["Type in the leave days each person had already taken before 1 January 2026 "
               "(ICAP students: total since the articleship began; Permanent / Internees: 0 — their 24 leaves restart each year)."])
    ws.append([])
    ws.append(["Emp ID", "Employee Name", "Staff Type", "Opening Balance (leaves taken before 1 Jan 2026)", "Of which Study Leave", "Remarks"])
    for c in ws[4]:
        c.font, c.fill = head
    for s in staff:
        ws.append([s["emp_id"], s["employee_name"], s["staff_type"], None, None, None])
        for col in "DEF":
            ws[f"{col}{ws.max_row}"].fill = entry
    for col, w in zip("ABCDEF", (9, 28, 14, 30, 20, 30)):
        ws.column_dimensions[col].width = w
    ws["A1"].font = Font(bold=True, size=13)

    lists = wb.create_sheet("Lists")  # drop-down sources (hidden)
    lists.append(["Employee Name", "Emp ID", "Leave Type"])
    for i in range(max(len(staff), len(types))):
        lists.append([staff[i]["employee_name"] if i < len(staff) else None, staff[i]["emp_id"] if i < len(staff) else None,
                      types[i] if i < len(types) else None])
    lists.sheet_state = "hidden"
    n_staff, n_types = len(staff) + 1, len(types) + 1

    lv = wb.create_sheet("Leaves")
    lv.append([f"{config.FIRM_NAME} — Leave Record from 1 January 2026 (enter each leave on its own row)"])
    lv.append(["Choose the Employee Name and Leave Type from the lists; Emp ID and Days fill in by themselves (type 0.5 in Days for half a day)."])
    lv.append(["Emp ID", "Employee Name", "Leave Type", "From", "To", "Days", "Reason"])
    for c in lv[3]:
        c.font, c.fill = head
    lv["A1"].font = Font(bold=True, size=13)
    name_dv = DataValidation(type="list", formula1=f"=Lists!$A$2:$A${n_staff}", allow_blank=True)
    type_dv = DataValidation(type="list", formula1=f"=Lists!$C$2:$C${n_types}", allow_blank=True)
    lv.add_data_validation(name_dv)
    lv.add_data_validation(type_dv)
    last = 3 + LEAVE_ROWS
    name_dv.add(f"B4:B{last}")
    type_dv.add(f"C4:C{last}")
    for r in range(4, last + 1):
        lv[f"A{r}"] = f'=IF(B{r}="","",IFERROR(VLOOKUP(B{r},Lists!$A$2:$B${n_staff},2,FALSE),""))'
        lv[f"F{r}"] = f'=IF(AND(ISNUMBER(D{r}),ISNUMBER(E{r})),E{r}-D{r}+1,"")'
        lv[f"D{r}"].number_format = lv[f"E{r}"].number_format = "dd/mm/yyyy"
        for col in "BCDEG":
            lv[f"{col}{r}"].fill = entry
    for col, w in zip("ABCDEFG", (9, 28, 30, 12, 12, 7, 36)):
        lv.column_dimensions[col].width = w
    lv.freeze_panes = "A4"

    sm = wb.create_sheet("Summary")  # running balance, worked out by formulas
    sm.append([f"{config.FIRM_NAME} — Leave balances (worked out from the other sheets)"])
    sm.append([])
    sm.append(["Emp ID", "Employee Name", "Staff Type", "Opening", "Taken 2026", "Of which Study (2026)",
               "Total Counted", "Entitlement", "Balance"])
    for c in sm[3]:
        c.font, c.fill = head
    sm["A1"].font = Font(bold=True, size=13)
    icap_total, annual = float(cfg.get("icap_articleship_leave_total", 130)), float(cfg.get("leaves_per_year_permanent", 24))
    for i, s in enumerate(staff):
        r, o = 4 + i, 5 + i
        sm.append([s["emp_id"], s["employee_name"], s["staff_type"]])
        sm[f"D{r}"] = f"='Opening Balance'!D{o}"
        sm[f"E{r}"] = f"=SUMIFS(Leaves!$F$4:$F${last},Leaves!$B$4:$B${last},B{r})"
        sm[f"F{r}"] = f'=SUMIFS(Leaves!$F$4:$F${last},Leaves!$B$4:$B${last},B{r},Leaves!$C$4:$C${last},"{STUDY_LEAVE_TYPE}")'
        icap = s["staff_type"] in ARTICLE_TYPES
        sm[f"G{r}"] = f"=D{r}+E{r}" if icap else f"=E{r}"
        sm[f"H{r}"] = (s["leave_entitlement_override"] if s["staff_type"] == NOC_TYPE and s["leave_entitlement_override"] is not None
                       else icap_total if icap else annual)
        sm[f"I{r}"] = f"=H{r}-G{r}"
    for col, w in zip("ABCDEFGHI", (9, 28, 14, 10, 12, 18, 13, 12, 10)):
        sm.column_dimensions[col].width = w
    sm.append([])
    sm.append([f"ICAP students: {icap_total:g} leaves over the whole articleship (opening + 2026). Permanent / Internees: {annual:g} a year. "
               f"Study leave and '{URGENT_WORK_ABROAD}' count towards the balance but are never deducted from salary."])

    notes = wb.create_sheet("Instructions")
    notes.column_dimensions["A"].width = 110
    for line in ("How to keep this leave record",
                 "1. Opening Balance: type in, for each person, the leave days taken before 1 January 2026 (and how many were study leave).",
                 "2. Leaves: one row per leave, from 1 January 2026 onward. Pick the Employee Name and the Leave Type from the drop-down lists:",
                 "   " + " | ".join(types),
                 "   Enter From and To (dd/mm/yyyy). Days is worked out (To − From + 1); overwrite it, e.g. 0.5 for half a day.",
                 "3. Summary shows each person's opening, 2026 leaves, study leave and balance — it updates by itself.",
                 "4. Do not rename the headings or the sheets.",
                 "5. To load it into the payroll: Leave Register → Upload Leave History → check the preview → Apply.",
                 "   The leaves are recorded as approved by the approver; the opening balance is used as at 1 January 2026."):
        notes.append([line])
    notes["A1"].font = Font(bold=True, size=12)
    wb.move_sheet("Instructions", offset=-(len(wb.sheetnames) - 1))  # first tab
    wb.active = 0
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

def read_leave_history(conn, content):
    """{opening: {emp: (days, study)}, leaves: [{emp_id, leave_type, date_from, date_to, days, reason}], problems: [..]}."""
    import io
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    staff = conn.execute("SELECT emp_id, employee_name FROM staff").fetchall()
    by_id = {s["emp_id"].upper(): s["emp_id"] for s in staff}
    by_name = {}
    for s in staff:
        by_name.setdefault(name_key(s["employee_name"]), []).append(s["emp_id"])
    def who(emp_cell, name_cell):
        e = by_id.get(str(emp_cell or "").strip().upper())
        if e:
            return e
        m = by_name.get(name_key(name_cell), [])
        return m[0] if len(m) == 1 else None
    out = {"opening": {}, "leaves": [], "problems": []}
    for ws in wb.worksheets:
        if ws.title.strip().lower().startswith(("instruction", "summary", "lists")):
            continue  # explanation, formula results and drop-down sources, not data
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        for h_at, row in enumerate(rows[:10]):
            heads = [" ".join(str(c or "").lower().split()) for c in row]
            if sum(1 for h in heads if h) < 3:
                continue  # titles / notes are single cells; the heading row has several
            col = lambda *words: next((i for i, h in enumerate(heads) if any(w in h for w in words)), None)
            c_emp, c_name = col("emp id", "emp. id", "emp code"), col("name")
            if c_emp is None and c_name is None:
                continue
            c_before, c_study = col("before", "opening"), col("of which study", "study leave")
            c_type, c_from, c_to, c_days, c_reason = col("type"), col("from"), col("to"), col("days"), col("reason", "remark")
            get = lambda r, i: r[i] if i is not None and i < len(r) else None
            if c_before is not None:  # opening balance sheet
                for r in rows[h_at + 1:]:
                    if get(r, c_emp) is None and get(r, c_name) is None:
                        continue
                    emp = who(get(r, c_emp), get(r, c_name))
                    days = _num(get(r, c_before))
                    if emp is None:
                        out["problems"].append(f"Opening Balance: '{get(r, c_emp) or get(r, c_name)}' is not a staff member")
                    elif days is not None:
                        out["opening"][emp] = (days, _num(get(r, c_study)) or 0)
            elif c_from is not None and c_type is not None:  # day-by-day leaves sheet
                for n, r in enumerate(rows[h_at + 1:], h_at + 2):
                    if all(get(r, i) in (None, "") for i in (c_emp, c_name, c_type, c_from)):
                        continue
                    emp = who(get(r, c_emp), get(r, c_name))
                    ltype = leave_type_from_text(get(r, c_type))
                    f, t = sheet_date(get(r, c_from)), sheet_date(get(r, c_to)) or sheet_date(get(r, c_from))
                    days = _num(get(r, c_days))
                    label = f"Leaves row {n}"
                    if emp is None:
                        out["problems"].append(f"{label}: '{get(r, c_emp) or get(r, c_name)}' is not a staff member")
                    elif ltype is None:
                        out["problems"].append(f"{label}: leave type '{get(r, c_type)}' not recognised")
                    elif f is None or t is None or t < f:
                        out["problems"].append(f"{label}: dates missing or To before From")
                    elif fmt_date(f) < LEAVE_HISTORY_FROM:
                        out["problems"].append(f"{label}: {fmt_date(f)} is before 1 Jan 2026 — include it in the opening balance instead")
                    else:
                        out["leaves"].append({"emp_id": emp, "leave_type": ltype, "date_from": fmt_date(f), "date_to": fmt_date(t),
                                              "days": days if days is not None else (t - f).days + 1,
                                              "reason": str(get(r, c_reason) or "").strip()[:200]})
            break
    if not out["opening"] and not out["leaves"]:
        raise ChangeError("leave_file_unreadable")
    return out

def apply_leave_history(conn, data, user, replace_existing):
    """Opening balances become 'as at 31 Dec 2025'; each leave is recorded as approved by the approver. With
    replace_existing, the staff's earlier 2026 leave records (except recorded absences) are replaced by the file's."""
    now = datetime.now().isoformat(timespec="seconds")
    cutoff = int(float(get_settings_dict(conn).get("cutoff_day", 25)))
    if data["opening"]:
        conn.execute("UPDATE settings SET value = '2025-12-31' WHERE key = 'opening_balances_as_at'")
        for emp, (days, _study) in data["opening"].items():
            conn.execute("UPDATE staff SET opening_leaves_cumulative = ?, opening_leaves_year = 0 WHERE emp_id = ?", (days, emp))
    if replace_existing:
        for emp in {l["emp_id"] for l in data["leaves"]}:
            conn.execute("DELETE FROM leave_register WHERE emp_id = ? AND date_from >= ? AND status != 'Recorded'", (emp, LEAVE_HISTORY_FROM))
    added = skipped = 0
    for l in data["leaves"]:
        if conn.execute("SELECT 1 FROM leave_register WHERE emp_id = ? AND leave_type = ? AND date_from = ? AND date_to = ?",
                        (l["emp_id"], l["leave_type"], l["date_from"], l["date_to"])).fetchone():
            skipped += 1
            continue
        absence = l["leave_type"] == ABSENCE_LEAVE_TYPE
        conn.execute("""INSERT INTO leave_register (application_date, emp_id, leave_type, date_from, date_to, no_of_days, reason, recommended_by,
                                                    approved_by, status, payroll_month, entered_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (l["date_from"], l["emp_id"], l["leave_type"], l["date_from"], l["date_to"], l["days"], l["reason"] or "Leave history upload",
                      None if absence else "Leave history upload", None if absence else user, "Recorded" if absence else "Approved",
                      fmt_date(get_payroll_month_for_event(parse_date(l["date_from"]), cutoff)), user))
        added += 1
    return added, skipped
