"""Timesheet, staff document and travel receipt downloads."""

import html as html_lib
import os

import config
from auth.roles import ROLE_STAFF
from common.constants import ARTICLE_TYPES, TS_CODE_FIELDS, TS_TEXT_FIELDS
from common.dates import parse_date, pay_month_label
from common.settings import get_settings_dict
from database import get_conn
from payroll.changes import training_org_of
from timesheets.rules import daily_hours, get_timesheet, timesheet_codes

class DownloadsMixin:
    def send_timesheet_view(self, path, qs, pm_str, role, own_emp):
        """The uploaded scan, or the printable daily diary, of one student's month. Staff may only see their own."""
        emp_id = own_emp if role == ROLE_STAFF else qs.get("emp", [""])[0].strip().upper()
        conn = get_conn()
        ts = get_timesheet(conn, emp_id, pm_str) if emp_id else None
        staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone() if emp_id else None
        if ts is None or staff is None:
            conn.close()
            return self.send_html("<p>No timesheet found. <a href='/?tab=timesheets'>Back</a></p>", status=404)

        if path == "/timesheet_file":
            conn.close()
            file_path = os.path.join(config.UPLOAD_DIR, ts["scan_file"] or "")
            if not ts["scan_file"] or not os.path.isfile(file_path):
                return self.send_html("<p>No scan uploaded. <a href='/?tab=timesheets'>Back</a></p>", status=404)
            with open(file_path, "rb") as f:
                content = f.read()
            safe_name = "".join(c for c in (ts["scan_name"] or "timesheet") if c.isalnum() or c in "._- ") or "timesheet"
            self.send_response(200)
            self.send_header("Content-Type", ts["scan_type"] or "application/octet-stream")
            self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        cfg = get_settings_dict(conn)
        entries = conn.execute("SELECT * FROM timesheet_entries WHERE timesheet_id = ? ORDER BY work_date", (ts["id"],)).fetchall()
        legend_desc = {cat: dict(lst) for cat, lst in timesheet_codes(conn).items()}
        conn.close()
        esc = html_lib.escape
        if ts["mode"] == "Daily":
            rows = ""
            for e in entries:
                d = parse_date(e["work_date"])
                off = e["day_type"] != "Work"
                if off:
                    rows += (f"<tr class='off'><td>{d.strftime('%d-%b-%y')}</td><td></td><td></td><td class='c'><b>{esc(e['day_type']).upper()}</b></td>"
                             "<td></td><td></td><td></td><td></td><td></td></tr>")
                else:
                    rows += (f"<tr><td>{d.strftime('%d-%b-%y')}</td><td>{esc(e['client_name'] or '')}</td><td class='c'>{esc(e['period_ended'] or '')}</td>"
                             f"<td>{esc(e['nature_of_work'] or '').replace(chr(10), '<br>')}</td><td class='c'>{esc(e['line_of_service'] or '')}</td>"
                             f"<td class='c'>{esc(e['head_of_account'] or '')}</td><td class='c'>{esc(e['work_performed'] or '')}</td>"
                             f"<td class='c'>{esc(e['job_code'] or '')}</td><td class='c'>{e['hours']:g}</td></tr>")
            working_days = round(ts["standard_hours"] / daily_hours(cfg)) if daily_hours(cfg) else 0
            used = [(TS_TEXT_FIELDS[cat], code, legend_desc.get(cat, {}).get(code, ""))
                    for cat in TS_CODE_FIELDS for code in sorted({e[cat] for e in entries if e[cat]})]
            legend = ("<p style='margin:14px 0 4px;'><b>Working Legends used:</b></p><table style='width:auto;'><tbody>"
                      + "".join(f"<tr><td>{esc(label)}</td><td class='c'>{esc(code)}</td><td>{esc(desc)}</td></tr>" for label, code, desc in used)
                      + "</tbody></table>") if used else ""
            table = f"""
            <table>
              <thead><tr><th colspan="9" class="black">DAILY DIARY</th></tr>
                <tr class="yellow"><th>Date</th><th>Client's Name</th><th>Year / Period Ended</th><th>Nature of work Undertaken/ Training Received</th>
                <th>Line of Service (1)</th><th>Head of account (2)</th><th>Work Performed (3)</th><th>Job Code (4)</th><th>HOURS</th></tr></thead>
              <tbody>{rows}</tbody>
            </table>
            <p class="tot"><span>Month End Total :</span><b>{ts['total_hours']:g}</b></p>
            <p class="tot"><span>Standard Hours in a month (No. of working days in the month X {daily_hours(cfg):g} hours/day)</span>
              <span>{working_days} &nbsp; {daily_hours(cfg):g}</span><b>{ts['standard_hours']:g}</b></p>{legend}"""
        else:
            table = f"""
            <p>Paper timesheet uploaded as a scan: <a href="/timesheet_file?ym={pm_str[:7]}&emp={esc(emp_id)}">{esc(ts['scan_name'] or 'view scan')}</a></p>
            <table style="width:auto;"><tbody>
              <tr><td>Total hours (as entered)</td><td class="c"><b>{ts['total_hours']:g}</b></td></tr>
              <tr><td>Leave days</td><td class="c">{ts['leave_days']:g}</td></tr>
              <tr><td>Study leave days</td><td class="c">{ts['study_days']:g}</td></tr>
            </tbody></table>"""

        def sign(label, who, when):
            return f"<div class='sig'><div class='line'>{esc(who or '')}<br><small>{(when or '')[:10]}</small></div>{label}</div>"

        page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
        <title>Daily Diary {esc(staff['employee_name'])} {pay_month_label(pm_str)}</title>
        <style>
          body {{ font-family: Calibri, 'Segoe UI', sans-serif; font-size: 12px; margin: 24px; color: #000; background: #fff; }}
          h1 {{ font-size: 13px; margin: 0; }} h2 {{ font-size: 13px; text-align: center; margin: 14px 0; }}
          .meta {{ display: flex; gap: 40px; margin-bottom: 4px; }}
          table {{ border-collapse: collapse; width: 100%; }}
          th, td {{ border: 1px solid #000; padding: 3px 5px; vertical-align: middle; }}
          .black {{ background: #000; color: #fff; }} .yellow th {{ background: #ffff00; }}
          tr.off td {{ background: #92d050; }} .c {{ text-align: center; }}
          .tot {{ display: flex; justify-content: space-between; margin: 6px 0; }}
          .sigs {{ display: flex; justify-content: space-between; margin-top: 50px; gap: 30px; }}
          .sig {{ flex: 1; text-align: center; }} .line {{ border-top: 1px solid #000; padding-top: 4px; min-height: 30px; }}
          .noprint {{ margin-bottom: 14px; }} @media print {{ .noprint {{ display: none; }} body {{ margin: 0; }} }}
        </style></head><body>
        <div class="noprint"><button onclick="window.print()">Print</button> &nbsp; <a href="/?tab=timesheets&ym={pm_str[:7]}&emp={esc(emp_id)}">Back</a>
          &nbsp; Status: <b>{esc(ts['status'])}</b></div>
        <h1>{esc(training_org_of(staff) if staff['staff_type'] in ARTICLE_TYPES else config.FIRM_NAME)}</h1>
        <h2>DAILY DIARY FOR THE MONTH OF {pay_month_label(pm_str)}</h2>
        {f'''<div class="meta"><span>NAME OF STUDENT: <b>{esc(staff['employee_name'])}</b></span><span>ICAP CRN # {esc(staff['icap_crn'] or '________')}</span>
          <span>DESIGNATION: TRAINEE STUDENT</span></div>''' if staff['staff_type'] in ARTICLE_TYPES else
         f'''<div class="meta"><span>NAME: <b>{esc(staff['employee_name'])}</b></span><span>EMP ID: {esc(staff['emp_id'])}</span>
          <span>DESIGNATION: {esc((staff['designation'] or staff['staff_type'] or '').upper())}</span></div>'''}
        {table}
        {f"<p><b>Remarks:</b> {esc(ts['remarks'])}</p>" if ts['remarks'] else ""}
        <div class="sigs">{sign("Student's Signature", staff['employee_name'], ts['submitted_at'])}{sign("Reviewed by", ts['reviewed_by'], ts['reviewed_at'])}{sign("Approved by", ts['approved_by'], ts['approved_at'])}</div>
        </body></html>"""
        return self.send_html(page)

    def send_staff_document(self, qs, role, own_emp):
        """A stored staff document (CNIC scan, CV, ...). Officers see all; staff only their own."""
        try:
            doc_id = int(qs.get("id", ["0"])[0])
        except ValueError:
            doc_id = 0
        conn = get_conn()
        d = conn.execute("SELECT * FROM staff_documents WHERE id = ?", (doc_id,)).fetchone()
        conn.close()
        if d is None or (role == ROLE_STAFF and d["emp_id"] != own_emp):
            return self.send_html("<p>Document not found. <a href='/'>Back</a></p>", status=404)
        path = os.path.join(config.UPLOAD_DIR, "documents", d["file"])
        if not os.path.isfile(path):
            return self.send_html("<p>The document file is missing. <a href='/'>Back</a></p>", status=404)
        with open(path, "rb") as f:
            content = f.read()
        safe_name = "".join(c for c in (d["original_name"] or "document") if c.isalnum() or c in "._- ") or "document"
        self.send_response(200)
        self.send_header("Content-Type", d["mime"] or "application/octet-stream")
        self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_travel_receipt(self, qs, role, own_emp):
        """Receipt / screenshot attached to a travel claim. Officers see all; staff only their own."""
        try:
            claim_id = int(qs.get("id", ["0"])[0])
        except ValueError:
            claim_id = 0
        conn = get_conn()
        t = conn.execute("SELECT emp_id, receipt_file, receipt_name, receipt_type FROM travel_claims WHERE id = ?", (claim_id,)).fetchone()
        conn.close()
        path = os.path.join(config.UPLOAD_DIR, "receipts", t["receipt_file"]) if t and t["receipt_file"] else ""
        if t is None or (role == ROLE_STAFF and t["emp_id"] != own_emp) or not os.path.isfile(path):
            return self.send_html("<p>Receipt not found. <a href='/?tab=travel'>Back</a></p>", status=404)
        with open(path, "rb") as f:
            content = f.read()
        safe_name = "".join(c for c in (t["receipt_name"] or "receipt") if c.isalnum() or c in "._- ") or "receipt"
        self.send_response(200)
        self.send_header("Content-Type", t["receipt_type"] or "application/octet-stream")
        self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)
