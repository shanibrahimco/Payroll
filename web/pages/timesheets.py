"""The timesheets tab of the web app."""

import html as html_lib

from attendance.records import month_holidays
from auth.roles import ROLE_ACTIONS, ROLE_APPROVER, ROLE_PREPARER, ROLE_REVIEWER
from common.constants import ARTICLE_TYPES, EOBI_ONLY_TYPE, NOC_TYPE, TS_CODE_FIELDS, TS_DAY_TYPES, TS_TEXT_FIELDS
from common.dates import fmt_date, month_days, parse_date, pay_month_label
from common.messages import FLASH_MESSAGES
from common.settings import get_settings_dict, is_month_locked
from payroll.changes import training_org_of
from timesheets.rules import (
    daily_hours,
    default_day_type,
    get_timesheet,
    register_leave_days,
    timesheet_codes,
    timesheet_holiday_work,
    timesheet_mode,
    timesheet_required,
)

def render(self, approver_only, cannot_decide, conn, is_staff, own_emp, pm_str, qs, role, ym, body=""):
    cfg = get_settings_dict(conn)
    mode = timesheet_mode(cfg, pm_str)
    esc = html_lib.escape
    emp = own_emp if is_staff else "".join(c for c in qs.get("emp", [""])[0].upper() if c.isalnum())
    status_css = {"Approved": "b-ok"}

    def status_badge(st):
        return f"<span class='badge {status_css.get(st, 'b-warn')}'>{st}</span>"

    list_panel = ""
    if not is_staff:
        students = [r for r in conn.execute("""
                    SELECT p.emp_id, p.employee_name, p.salary_withheld_timesheet, p.timesheet_salary_released, t.status, t.total_hours,
                           t.standard_hours, t.leave_days, t.study_days, t.submitted_at, t.reviewed_by, t.approved_by, t.mode,
                           s.staff_type, s.timesheet_from
                    FROM payroll_lines p JOIN staff s ON s.emp_id = p.emp_id
                    LEFT JOIN timesheets t ON t.emp_id = p.emp_id AND t.payroll_month = p.payroll_month
                    WHERE p.payroll_month = ? AND p.on_payroll = 1 ORDER BY p.emp_id
                """, (pm_str,)).fetchall() if timesheet_required(r, pm_str, cfg)]

        def student_row(r):
            reg_total, reg_study = register_leave_days(conn, r["emp_id"], pm_str)
            sheet_leave = f"{(r['leave_days'] or 0) + (r['study_days'] or 0):g}" if r["status"] else "—"
            held = (f"<span class='badge b-warn'>{r['salary_withheld_timesheet']:,.0f}</span>" if r["salary_withheld_timesheet"] else "0")
            released = f"<br><span class='note'>+{r['timesheet_salary_released']:,.0f} released</span>" if r["timesheet_salary_released"] else ""
            asked = "" if r["staff_type"] in ARTICLE_TYPES else "<br><span class='note'>asked by reviewer</span>"
            return f"""<tr><td>{r['emp_id']}</td><td><strong>{esc(r['employee_name'])}</strong>{asked}</td>
                      <td>{status_badge(r['status'] or 'Not started')}</td><td>{r['mode'] or ''}</td>
                      <td class="num">{f"{r['total_hours']:g}" if r['status'] else '—'}</td>
                      <td class="num">{sheet_leave} / {reg_total:g}</td>
                      <td>{(r['submitted_at'] or '')[:10]}</td><td>{esc(r['reviewed_by'] or '')}</td><td>{esc(r['approved_by'] or '')}</td>
                      <td class="num">{held}{released}</td>
                      <td><a class="btn btn-sec" href="/?tab=timesheets&ym={ym}&emp={r['emp_id']}">Open</a></td></tr>"""

        waiting = {ROLE_REVIEWER: "Submitted", ROLE_APPROVER: "Reviewed"}.get(role)
        n_waiting = sum(1 for r in students if r["status"] == waiting) if waiting else 0
        list_panel = f"""
                <div class="panel">
                  <h3>Timesheets — {pay_month_label(pm_str)}</h3>
                  <p class="note">Each ICAP student (and any other staff member the reviewer has asked) submits a monthly daily diary. It is <b>reviewed by the reviewer</b> and then
                    <b>approved by the approver</b>. Until it is approved the student's whole salary for that month is held; approval
                    releases it (in the same month's payroll while it is still Draft, otherwise automatically in the next one).
                    {"This month is a <b>paper sheet</b>: the student uploads the scan and types in the totals." if mode == "Scanned" else "This month is filled in <b>day by day</b> in the system."}
                    {f"<b>{n_waiting} timesheet(s) waiting for you.</b>" if n_waiting else ""}</p>
                  <table>
                    <thead><tr><th>ID</th><th>Name</th><th>Status</th><th>Type</th><th class="num">Hours</th><th class="num">Leave Days (Sheet / Register)</th>
                      <th>Submitted</th><th>Reviewed By</th><th>Approved By</th><th class="num">Salary Held</th><th></th></tr></thead>
                    <tbody>{"".join(student_row(r) for r in students) or "<tr><td colspan='11'>No timesheets are required for this month.</td></tr>"}</tbody>
                  </table>
                </div>"""
        if role in ROLE_ACTIONS["/set_ts_access"]:
            others = conn.execute("""SELECT emp_id, employee_name, staff_type, timesheet_from FROM staff
                                             WHERE status = 'Active' AND COALESCE(staff_type, '') NOT IN ('ICAP Article', ?, ?) ORDER BY emp_id""",
                                  (NOC_TYPE, EOBI_ONLY_TYPE)).fetchall()
            enabled = [o for o in others if o["timesheet_from"]]
            emp_opts = "".join(f"<option value='{o['emp_id']}'>{o['emp_id']} — {esc(o['employee_name'])} ({esc(o['staff_type'] or '')})</option>" for o in others)
            enabled_rows = "".join(
                f"<tr><td>{o['emp_id']}</td><td>{esc(o['employee_name'])}</td><td>{esc(o['staff_type'] or '')}</td><td>from {pay_month_label(o['timesheet_from'])}</td>"
                f"<td><form method='POST' action='/set_ts_access?ym={ym}'><input type='hidden' name='emp_id' value='{o['emp_id']}'>"
                f"<input type='hidden' name='enable' value='0'><button class='btn btn-danger' type='submit'>No Longer Required</button></form></td></tr>"
                for o in enabled)
            list_panel += f"""
                <div class="panel">
                  <h3>Timesheets for Other Staff</h3>
                  <p class="note">Timesheets are required only from ICAP students. If the reviewer needs one from another staff member, enable it
                    here: from the chosen month that person gets the Timesheets tab and the <b>same rules apply</b> (submitted, reviewed, approved;
                    salary held until approved). Every change is recorded in the Approval Log.</p>
                  <form class="inline" method="POST" action="/set_ts_access?ym={ym}">
                    <label>Staff Member <select name="emp_id" required>{emp_opts}</select></label>
                    <label>Required From <input type="month" name="from_month" value="{ym}" required></label>
                    <input type="hidden" name="enable" value="1">
                    <button class="btn" type="submit">Require Timesheets</button>
                  </form>
                  {f"<table><thead><tr><th>ID</th><th>Name</th><th>Type</th><th>Timesheets</th><th></th></tr></thead><tbody>{enabled_rows}</tbody></table>" if enabled else "<p class='note'>No other staff are currently asked for timesheets.</p>"}
                </div>"""

    detail = ""
    staff_row = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp,)).fetchone() if emp else None
    if is_staff and not own_emp:
        detail = f"<div class='panel'><p class='note'>{FLASH_MESSAGES['no_emp_link'][1]}</p></div>"
    elif staff_row is not None and not timesheet_required(staff_row, pm_str, cfg) and not get_timesheet(conn, emp, pm_str):
        detail = f"<div class='panel'><p class='note'>{FLASH_MESSAGES['ts_not_icap'][1]}</p></div>"
    elif staff_row is not None:
        ts = get_timesheet(conn, emp, pm_str)
        st = ts["status"] if ts else "Not started"
        can_edit = (role in (ROLE_APPROVER, ROLE_PREPARER) or (is_staff and emp == own_emp)) and st in ("Not started", "Draft", "Returned")
        ts_mode = ts["mode"] if ts else mode
        ro = "" if can_edit else " disabled"
        reg_total, reg_study = register_leave_days(conn, emp, pm_str)
        form_action = f"/save_timesheet?ym={ym}&emp={emp}"

        trail = []
        if ts and ts["submitted_at"]:
            trail.append(f"Submitted {ts['submitted_at'][:16].replace('T', ' ')} by {esc(ts['prepared_by'] or '')}")
        if ts and ts["reviewed_by"]:
            trail.append(f"Reviewed by <b>{esc(ts['reviewed_by'])}</b> {ts['reviewed_at'][:16].replace('T', ' ')}")
        if ts and ts["approved_by"]:
            trail.append(f"Approved by <b>{esc(ts['approved_by'])}</b> {ts['approved_at'][:16].replace('T', ' ')}")
        returned = (f"<div class='flash flash-warn'>Returned by {esc(ts['returned_by'] or '')}: {esc(ts['return_note'] or '')}</div>"
                    if st == "Returned" else "")
        links = ""
        if ts:
            links = f"<a class='btn btn-sec' href='/timesheet_print?ym={ym}&emp={emp}' target='_blank'>Print Daily Diary</a>"
            if ts["scan_file"]:
                links += f" <a class='btn btn-sec' href='/timesheet_file?ym={ym}&emp={emp}' target='_blank'>View Uploaded Scan</a>"

        buttons = (f"<button class='btn' type='submit' name='action' value='save'>Save Draft</button>"
                   f"<button class='btn btn-ok' type='submit' name='action' value='submit'>Submit for Review</button>") if can_edit else ""

        if ts_mode == "Scanned":
            current = (f"Uploaded: <a href='/timesheet_file?ym={ym}&emp={emp}' target='_blank'>{esc(ts['scan_name'] or 'scan')}</a>"
                       if ts and ts["scan_file"] else "No scan uploaded yet.")
            form = f"""
                    <p class="note">For {pay_month_label(pm_str)} the timesheet is the paper sheet. Scan it clearly, upload it (PDF, JPG or PNG,
                      up to 10 MB) and type in the month-end total hours and leave days from it. The reviewer checks the figures against the scan.
                      {current}</p>
                    <form class="inline" method="POST" action="{form_action}" enctype="multipart/form-data">
                      <label>Scanned Timesheet <input type="file" name="scan" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"{ro}></label>
                      <label>Month End Total Hours <input type="number" step="0.5" min="0" name="total_hours" value="{(ts['total_hours'] if ts else 0):g}" style="width:110px;"{ro}></label>
                      <label>Leave Days <input type="number" step="0.5" min="0" name="leave_days" value="{(ts['leave_days'] if ts else 0):g}" style="width:80px;"{ro}></label>
                      <label>Study Leave Days <input type="number" step="0.5" min="0" name="study_days" value="{(ts['study_days'] if ts else 0):g}" style="width:80px;"{ro}></label>
                      <label>Remarks <input name="remarks" value="{esc(ts['remarks'] or '') if ts else ''}" size="30"{ro}></label>
                      {buttons}
                    </form>"""
        else:
            entries = {}
            if ts:
                entries = {e["work_date"]: e for e in conn.execute("SELECT * FROM timesheet_entries WHERE timesheet_id = ?", (ts["id"],))}
            cell = "padding:3px 5px; font-size:12px;"
            ts_codes = timesheet_codes(conn)
            hols = month_holidays(conn, pm_str)
            grid = ""
            for d in month_days(pm_str):
                e = entries.get(fmt_date(d))
                k = d.day
                day_type = e["day_type"] if e else default_day_type(d, hols)
                hol_name = hols.get(fmt_date(d))
                opts = "".join(f"<option{' selected' if t == day_type else ''}>{t}</option>" for t in TS_DAY_TYPES)

                def txt(col, width):
                    return f"<td><input name='{col}_{k}' value='{esc(e[col] or '', quote=True) if e else ''}' style='{cell} width:{width}px;'{ro}></td>"

                def pick(col):
                    current = (e[col] or "") if e else ""
                    options = "<option value=''></option>" + "".join(
                        f"<option value='{esc(c, quote=True)}' title='{esc(desc, quote=True)}'{' selected' if c == current else ''}>"
                        f"{esc(c)}{' — ' + esc(desc) if desc else ''}</option>" for c, desc in ts_codes[col])
                    return f"<td><select name='{col}_{k}' style='{cell} max-width:120px;'{ro}>{options}</select></td>"
                worked_holiday = hol_name and day_type == "Work"
                bg = (" style='background:#fef3c7;'" if worked_holiday else " style='background:#ecfccb;'" if day_type != "Work" else "")
                hol_label = ""
                if hol_name:
                    hol_label = (f"<br><span class='note'><b>{esc(hol_name)}</b>"
                                 f"{' — worked: apply for credit leave within 7 days' if worked_holiday else ''}</span>")
                grid += (f"<tr{bg}><td>{d.strftime('%d-%b')} <span class='note'>{d.strftime('%a')}</span>{hol_label}</td>"
                         f"<td><select name='type_{k}' style='{cell}'{ro}>{opts}</select></td>"
                         + txt("client_name", 140) + txt("period_ended", 50) + txt("nature_of_work", 220) + pick("line_of_service")
                         + pick("head_of_account") + pick("work_performed") + txt("job_code", 70)
                         + f"<td><input type='number' step='0.5' min='0' max='24' name='hours_{k}' value='{(e['hours'] if e and e['hours'] else '') if e else ''}' style='{cell} width:55px;'{ro}></td></tr>")
            totals = (f"Month end total: <b>{ts['total_hours']:g} hours</b> &nbsp;|&nbsp; Standard hours: <b>{ts['standard_hours']:g}</b> "
                      f"(working days × {daily_hours(cfg):g} h) &nbsp;|&nbsp; Leave: {ts['leave_days']:g} &nbsp;|&nbsp; Study leave: {ts['study_days']:g}"
                      if ts else "Totals are worked out when you save.")
            form = f"""
                    <p class="note"><b>Gazetted holidays and weekends are filled in automatically.</b> If your client didn't allow a gazetted
                      holiday, change that day to <b>Work</b> and enter the work. To earn credit leave for a complete working day on a gazetted holiday, Saturday or Sunday, apply on the Leave Register within 7 days.
                      Fill in one row per day, as on the paper daily diary. Choose the day type; only <b>Work</b> days take a client,
                      nature of work and hours. Choose Line of Service, Head of Account and Work Performed from the lists (the working
                      legends); if a code is missing, ask the reviewer to add it. Save as draft any time; submit once the month is complete.</p>
                    <form method="POST" action="{form_action}">
                      <div style="overflow-x:auto;"><table>
                        <thead><tr><th>Date</th><th>Day Type</th><th>Client's Name</th><th>Year / Period</th><th>Nature of Work / Training</th>
                          <th>Line of Service</th><th>Head of A/c</th><th>Work Perf.</th><th>Job Code</th><th>Hours</th></tr></thead>
                        <tbody>{grid}</tbody>
                      </table></div>
                      <p class="note" style="margin-top:10px;">{totals}</p>
                      <label style="max-width:500px;">Remarks <input name="remarks" value="{esc(ts['remarks'] or '') if ts else ''}"{ro}></label>
                      <div style="display:flex; gap:8px; margin-top:12px;">{buttons}</div>
                    </form>"""

        decide = ""
        if role in ROLE_ACTIONS["/decide_timesheet"] and ts and not cannot_decide(emp):
            steps = []
            if role == ROLE_REVIEWER and st == "Submitted":
                steps.append(("Reviewed", "Mark as Reviewed"))
            if role == ROLE_APPROVER and (st == "Reviewed" or (st == "Submitted" and emp in approver_only)):
                steps.append(("Approved", "Approve Timesheet & Release Salary"))
            can_return = st in ("Submitted", "Reviewed") or (st == "Approved" and role == ROLE_APPROVER and not is_month_locked(conn, pm_str))
            btns = "".join(f"<form method='POST' action='/decide_timesheet?ym={ym}&emp={emp}'><input type='hidden' name='decision' value='{d}'>"
                           f"<button class='btn btn-ok' type='submit'>{label}</button></form>" for d, label in steps)
            if can_return:
                btns += (f"<form method='POST' action='/decide_timesheet?ym={ym}&emp={emp}' style='display:flex; gap:6px; align-items:flex-end;'>"
                         f"<input type='hidden' name='decision' value='Returned'><label>Note to student <input name='note' size='34' required></label>"
                         f"<button class='btn btn-danger' type='submit'>Return for Correction</button></form>")
            if btns:
                sheet_leave = (ts["leave_days"] or 0) + (ts["study_days"] or 0)
                check = (f"Leave register shows <b>{reg_total:g}</b> day(s) this month ({reg_study:g} study); the timesheet shows "
                         f"<b>{sheet_leave:g}</b> ({ts['study_days']:g} study).")
                if abs(reg_total - sheet_leave) > 0.01:
                    check = f"<span class='badge b-warn'>Mismatch</span> {check}"
                worked = timesheet_holiday_work(conn, ts)
                if worked:
                    check += (f"<br><b>Gazetted holiday(s) worked at client:</b> "
                              + ", ".join(f"{parse_date(w['work_date']).strftime('%d %b')} ({esc(w['name'])})" for w in worked)
                              + " — credit leave needs the student's own application on the Leave Register.")
                decide = f"""<div class="panel"><h3>Review / Approval</h3><p class="note">{check}</p>
                          <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:flex-end;">{btns}</div></div>"""

        detail = f"""
                <div class="panel">
                  <div class="runbar" style="margin-bottom:10px;">
                    <h3 style="margin:0;">Daily Diary — {esc(staff_row['employee_name'])} ({emp}) · {pay_month_label(pm_str)} &nbsp; {status_badge(st)}</h3>
                    <div>{links}</div>
                  </div>
                  <p class="note">ICAP CRN: {esc(staff_row['icap_crn'] or 'not entered')} &nbsp;|&nbsp; {('Training organisation: ' + esc(training_org_of(staff_row)) + ' &nbsp;|&nbsp; ') if staff_row['staff_type'] in ARTICLE_TYPES else ''}{' &nbsp;|&nbsp; '.join(trail) or 'Not submitted yet.'}</p>
                  {returned}
                  {form}
                </div>{decide}"""
    elif emp:
        detail = f"<div class='panel'><p class='note'>{FLASH_MESSAGES['emp_missing'][1]}</p></div>"

    history = ""
    if is_staff and own_emp:
        past = conn.execute("SELECT payroll_month, status, total_hours FROM timesheets WHERE emp_id = ? ORDER BY payroll_month DESC", (own_emp,)).fetchall()
        if past:
            history = f"""<div class="panel"><h3>My Timesheets</h3><table>
                      <thead><tr><th>Month</th><th>Status</th><th class="num">Hours</th><th></th></tr></thead><tbody>
                      {"".join(f"<tr><td>{pay_month_label(p['payroll_month'])}</td><td>{status_badge(p['status'])}</td><td class='num'>{p['total_hours']:g}</td><td><a class='btn btn-sec' href='/?tab=timesheets&ym={p['payroll_month'][:7]}'>Open</a></td></tr>" for p in past)}
                      </tbody></table></div>"""
    legend_lists = timesheet_codes(conn)
    legend_cols = "".join(
        f"<div style='min-width:220px;'><h4 style='margin:0 0 6px 0;'>{TS_TEXT_FIELDS[cat]}</h4><table><tbody>"
        + "".join(f"<tr><td><b>{esc(c)}</b></td><td>{esc(desc) or '<span class=note>(no description)</span>'}</td></tr>" for c, desc in legend_lists[cat])
        + "</tbody></table></div>" for cat in TS_CODE_FIELDS)
    add_code = ""
    if role in ROLE_ACTIONS["/save_ts_code"]:
        cat_opts = "".join(f"<option value='{cat}'>{TS_TEXT_FIELDS[cat]}</option>" for cat in TS_CODE_FIELDS)
        add_code = f"""
                  <form class="inline" method="POST" action="/save_ts_code?ym={ym}" style="margin-top:14px;">
                    <label>List <select name="category">{cat_opts}</select></label>
                    <label>Code <input name="code" required size="8" placeholder="1100"></label>
                    <label>Description <input name="description" size="40" placeholder="e.g. Audit &amp; Assurance"></label>
                    <button class="btn" type="submit">Add / Update Code</button>
                  </form>
                  <p class="note">Entering an existing code updates its description. Every addition is recorded in the Approval Log.</p>"""
    legends_panel = f"""
            <div class="panel"><details{' open' if add_code and not emp else ''}>
              <summary style="cursor:pointer; font-weight:600;">Working Legends — timesheet drop-down codes</summary>
              <div style="display:flex; gap:24px; flex-wrap:wrap; margin-top:12px; align-items:flex-start;">{legend_cols}</div>
              {add_code}
            </details></div>"""
    body = detail + list_panel + history + legends_panel
    return body
