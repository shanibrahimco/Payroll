"""Final payroll sheet import (Excel prepared outside the system)."""

from datetime import datetime
import csv
import json

import config
from attendance.records import name_key
from common.dates import parse_date, pay_month_label
from common.errors import ChangeError
from common.settings import is_month_locked
from payroll.calculator import calculate_payroll

SHEET_FIELDS = (  # payroll field, heading keywords (checked in this order, most specific first)
    ("net_pay", ("net pay", "net salary", "net payable", "net")),
    ("gross_earned", ("gross",)),
    ("leave_deduction", ("leave ded", "leave deduction")),
    ("salary_withheld_probation", ("prob. withheld", "probation withheld", "prob withheld", "withheld")),
    ("probation_salary_released", ("prob. rel", "probation released", "prob rel")),
    ("loan_advance_recovery", ("loan", "advance")),
    ("eobi_cost_accrued", ("eobi",)),
    ("income_tax_deduction", ("tax",)),
    ("travelling_allowance", ("travel", "conveyance", "t.a")),
    ("bonus", ("bonus",)),
    ("arrears_other", ("arrear",)),
    ("late_arrival", ("late",)),
    ("leaves_this_month", ("leaves (mo", "leaves this month", "leave days", "leaves")),
    ("days_employed", ("days",)),
    ("monthly_salary", ("monthly salary", "basic salary", "salary")),
    ("emp_id", ("emp id", "emp. id", "employee id", "emp code", "emp#", "emp no", "id")),
    ("employee_name", ("employee name", "name", "employee")),
)

SHEET_LABELS = {"net_pay": "Net Pay", "gross_earned": "Gross", "leave_deduction": "Leave Ded.", "salary_withheld_probation": "Prob. Withheld",
                "probation_salary_released": "Prob. Released", "loan_advance_recovery": "Loan Recovery", "eobi_cost_accrued": "EOBI",
                "income_tax_deduction": "Tax", "travelling_allowance": "Travel", "bonus": "Bonus", "arrears_other": "Arrears",
                "late_arrival": "Late Ded.", "leaves_this_month": "Leaves (Mo.)", "days_employed": "Days", "monthly_salary": "Salary",
                "emp_id": "Emp ID", "employee_name": "Name"}

NET_ADD = ("gross_earned", "travelling_allowance", "bonus", "arrears_other", "probation_salary_released")

NET_LESS = ("leave_deduction", "late_arrival", "loan_advance_recovery", "salary_withheld_probation", "income_tax_deduction")

TEMPLATE_COLUMNS = (  # heading (as the upload recognises it), payroll field or None, width
    ("Sr", None, 5), ("Emp ID", "emp_id", 8), ("Employee Name", "employee_name", 26), ("Staff Type", "staff_type", 13),
    ("Monthly Salary", "monthly_salary", 13), ("Days", "days_employed", 7), ("Gross Salary", "gross_earned", 13),
    ("Travelling", "travelling_allowance", 11), ("Bonus", "bonus", 9), ("Arrears", "arrears_other", 9),
    ("Prob. Released", "probation_salary_released", 12), ("Leave Ded.", "leave_deduction", 11),
    ("Loan Rec.", "loan_advance_recovery", 10), ("Prob. Withheld", "salary_withheld_probation", 12), ("Tax", "income_tax_deduction", 8),
    ("Net Pay", "net_pay", 13), ("EOBI (Firm)", "eobi_cost_accrued", 11), ("Remarks", None, 30),
)

def build_payroll_template(conn, pm_str):
    """Excel template for a month's final payroll sheet, pre-filled with the staff on that month's payroll and the system's
    current figures. Net Pay is a formula; the headings are exactly the ones the upload recognises."""
    import io
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    lines = conn.execute("""SELECT p.*, s.staff_type AS stype FROM payroll_lines p JOIN staff s ON s.emp_id = p.emp_id
                            WHERE p.payroll_month = ? AND p.on_payroll = 1 ORDER BY p.emp_id""", (pm_str,)).fetchall()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = parse_date(pm_str).strftime("%b-%y") + " Payroll"
    bold, white = Font(bold=True), Font(bold=True, color="FFFFFF")
    head_fill, input_fill = PatternFill("solid", fgColor="0F172A"), PatternFill("solid", fgColor="FEF9C3")
    thin = Side(style="thin", color="CBD5E1")
    ws["A1"] = config.FIRM_NAME
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = f"Final Payroll — {pay_month_label(pm_str)}"
    ws["A2"].font = Font(bold=True, size=12)
    ws["A3"] = "Prepared by: ____________________   Date: __________   (Upload: Monthly Payroll → Upload Final Payroll Sheet)"
    head_row = 5
    col_of = {}
    for c, (heading, field, width) in enumerate(TEMPLATE_COLUMNS, 1):
        cell = ws.cell(head_row, c, heading)
        cell.font, cell.fill = white, head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(c)].width = width
        col_of[heading] = get_column_letter(c)
    first = head_row + 1
    for i, l in enumerate(lines):
        r = first + i
        for c, (heading, field, _) in enumerate(TEMPLATE_COLUMNS, 1):
            if heading == "Sr":
                value = i + 1
            elif field == "staff_type":
                value = l["stype"]
            elif heading == "Net Pay":
                plus = "+".join(f"{col_of[h]}{r}" for h, f, _ in TEMPLATE_COLUMNS if f in NET_ADD)
                minus = "-".join(f"{col_of[h]}{r}" for h, f, _ in TEMPLATE_COLUMNS if f in NET_LESS)
                value = f"={plus}-{minus}"
            elif field == "probation_salary_released":
                value = l[field] + l["security_released"]  # includes held pay / security released to a leaver
            elif field:
                value = l[field]
            else:
                value = None
            cell = ws.cell(r, c, value)
            cell.border = Border(top=thin, bottom=thin, left=thin, right=thin)
            if field in NET_ADD + NET_LESS or field in ("days_employed", "monthly_salary", "eobi_cost_accrued") or heading == "Remarks":
                cell.fill = input_fill  # figures the reviewer finalises
            if field not in (None, "emp_id", "employee_name", "staff_type") or heading == "Net Pay":
                cell.number_format = "#,##0;(#,##0);-"
            if heading == "Net Pay":
                cell.font = bold
    total_row = first + len(lines)
    ws.cell(total_row, 3, "TOTAL").font = bold
    for heading, field, _ in TEMPLATE_COLUMNS:
        if field and field not in ("emp_id", "employee_name", "staff_type", "days_employed"):
            letter = col_of[heading]
            cell = ws[f"{letter}{total_row}"]
            cell.value = f"=SUM({letter}{first}:{letter}{total_row - 1})"
            cell.font, cell.number_format = bold, "#,##0;(#,##0);-"
    ws.freeze_panes = ws.cell(first, 4)
    ws.print_title_rows = f"{head_row}:{head_row}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1

    notes = wb.create_sheet("Instructions")
    notes.column_dimensions["A"].width = 110
    for i, text in enumerate((
        f"How to use this template — {pay_month_label(pm_str)}",
        "",
        "1. The rows are pre-filled with the staff on this month's payroll and the system's current figures. Change any figure to the final amount.",
        "2. Yellow cells are the figures to finalise. Net Pay is calculated: Gross + Travelling + Bonus + Arrears + Prob. Released",
        "   − Leave Ded. − Loan Rec. − Prob. Withheld − Tax. You may overwrite Net Pay with a fixed amount if needed.",
        "3. Do NOT rename the column headings, change Emp IDs, or delete the heading row. Extra columns (e.g. Remarks) are ignored.",
        "4. A person who must not be paid this month: put 0 in Net Pay (or delete the row — anyone not on the sheet is paid nothing).",
        "5. A new person must first be added in Staff Master (with approval) — rows with an unknown Emp ID are not imported.",
        "6. Save the file in Excel (so the formulas are calculated) and give it to the approver.",
        "7. Approver: Monthly Payroll → choose the month → Upload Final Payroll Sheet → check the preview → Apply.",
        "   The month is then approved and locked with these figures; later months follow the system's rules.",
    ), 1):
        notes.cell(i, 1, text).font = Font(bold=(i == 1), size=12 if i == 1 else 11)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("Rs.", "").replace("Rs", "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None

def map_sheet_headings(headings):
    """{column index: payroll field} from a heading row; each field is used once."""
    mapping, used = {}, set()
    for i, h in enumerate(headings):
        text = " ".join(str(h or "").lower().split())
        if not text:
            continue
        for field, words in SHEET_FIELDS:
            if field in used:
                continue
            if any((w == text) if w in ("id", "net", "name") else (w in text) for w in words):
                mapping[i] = field
                used.add(field)
                break
    return mapping

def read_payroll_sheet(conn, filename, content):
    """Find the sheet / heading row of a final payroll sheet and match its rows to staff.
    Returns (sheet name, {heading: field}, {emp_id: {field: value}}, [unmatched row labels])."""
    import io
    if filename.lower().endswith((".xlsx", ".xlsm")) or content.startswith(b"PK\x03\x04"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheets = [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)]) for ws in wb.worksheets]
    else:
        sheets = [("CSV", [list(r) for r in csv.reader(content.decode("utf-8-sig", "replace").splitlines())])]
    staff = conn.execute("SELECT emp_id, employee_name FROM staff").fetchall()
    by_id = {s["emp_id"].upper(): s["emp_id"] for s in staff}
    by_name = {}
    for s in staff:
        by_name.setdefault(name_key(s["employee_name"]), []).append(s["emp_id"])
    best = None
    for title, rows in sheets:
        for h_at, row in enumerate(rows[:20]):
            mapping = map_sheet_headings(row)
            if "net_pay" not in mapping.values() or not ({"emp_id", "employee_name"} & set(mapping.values())):
                continue
            col = {f: i for i, f in mapping.items()}
            matched, unmatched = {}, []
            for r in rows[h_at + 1:]:
                cell = lambda f: r[col[f]] if f in col and col[f] < len(r) else None
                raw_id = str(cell("emp_id") or "").strip().upper()
                raw_name = " ".join(str(cell("employee_name") or "").lower().split())
                if not raw_id and not raw_name:
                    continue
                if raw_name in ("total", "grand total", "totals") or raw_id in ("TOTAL", "GRAND TOTAL"):
                    continue
                emp = by_id.get(raw_id) or (lambda m: m[0] if len(m) == 1 else None)(by_name.get(name_key(raw_name), []))
                values = {f: _num(cell(f)) for f in col if f not in ("emp_id", "employee_name")}
                if values.get("net_pay") is None and any(values.get(f) is not None for f in NET_ADD + NET_LESS):
                    # Net Pay left as a formula never calculated (file not saved in Excel): work it out the same way
                    values["net_pay"] = (sum(values.get(f) or 0 for f in NET_ADD) - sum(values.get(f) or 0 for f in NET_LESS))
                if emp is None:
                    if values.get("net_pay") is not None:
                        unmatched.append(" / ".join(x for x in (raw_id, str(cell("employee_name") or "")) if x))
                    continue
                matched[emp] = values
            if best is None or len(matched) > len(best[2]):
                best = (title, {str(rows[h_at][i]).strip(): f for i, f in mapping.items()}, matched, unmatched)
            break  # first qualifying heading row of this sheet
    if best is None or not best[2]:
        raise ChangeError("sheet_unreadable")
    return best

def apply_payroll_sheet(conn, imp, user, update_salaries=False):
    """Replace the month's payroll with the sheet's figures, then approve and lock the month."""
    pm_str = imp["payroll_month"]
    rows = json.loads(imp["rows"])
    now = datetime.now().isoformat(timespec="seconds")
    if not is_month_locked(conn, pm_str):
        calculate_payroll(pm_str)  # start from up-to-date lines for everyone, then overwrite with the sheet
    banks ={r["bank_name"]: dict(r) for r in conn.execute("SELECT * FROM banks")}
    for line in conn.execute("SELECT * FROM payroll_lines WHERE payroll_month = ?", (pm_str,)).fetchall():
        emp = line["emp_id"]
        if emp in rows:
            values = {f: v for f, v in rows[emp].items() if v is not None and f in SHEET_LABELS}
            if "probation_salary_released" in values:  # the sheet's column includes any security released to a leaver
                values["probation_salary_released"] = max(0.0, values["probation_salary_released"] - line["security_released"])
            note = f"Final figures from {imp['file_name']}."
        elif line["on_payroll"]:
            values = {"net_pay": 0.0}  # paid only if on the final sheet
            note = f"Not on the final sheet {imp['file_name']}: nothing paid."
        else:
            continue  # EOBI-only / not employed: keep as calculated
        net = values.get("net_pay", line["net_pay"])
        s = conn.execute("SELECT bank_name FROM staff WHERE emp_id = ?", (emp,)).fetchone()
        b = banks.get(s["bank_name"] or "", {}) if s else {}
        if line["payment_mode"] == "EOBI only":
            mode = "EOBI only"
        elif net <= 0:
            mode = "Nothing to pay"
        else:
            mode = "Meezan IFT" if (b.get("is_meezan") == 1 or (s and s["bank_name"] == "Meezan Bank")) else "IBFT"
        values.update(payment_mode=mode, validation_checks=note,
                      salary_withheld_timesheet=0.0, timesheet_salary_released=0.0, salary_withheld_absence=0.0, absence_salary_released=0.0,
                      salary_withheld_late=0.0, late_salary_released=0.0)
        conn.execute(f"UPDATE payroll_lines SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ?", (*values.values(), line["id"]))
        if update_salaries and rows.get(emp, {}).get("monthly_salary"):
            conn.execute("UPDATE staff SET monthly_salary = ? WHERE emp_id = ? AND weekly_hours IS NULL", (rows[emp]["monthly_salary"], emp))
    conn.execute("""UPDATE payroll_runs SET status = 'Approved', prepared_by = ?, prepared_date = ?, reviewed_by = ?, reviewed_date = ?,
                    approved_by = ?, approved_date = ?, imported_from = ? WHERE payroll_month = ?""",
                 (f"Final sheet: {imp['file_name']}", imp["uploaded_at"], "Final sheet (prepared outside the system)", imp["uploaded_at"],
                  user, now, imp["file_name"], pm_str))
    conn.execute("UPDATE payroll_imports SET status = 'Discarded' WHERE payroll_month = ? AND status = 'Applied'", (pm_str,))
    conn.execute("UPDATE payroll_imports SET status = 'Applied', applied_at = ? WHERE id = ?", (now, imp["id"]))
