"""The payroll tab of the web app."""

import html as html_lib
import json

from attendance.timing import late_arrival_days, late_days_text
from auth.roles import ROLE_ACTIONS, ROLE_APPROVER, ROLE_REVIEWER
from common.constants import ARTICLE_TYPES, EOBI_ONLY_TYPE
from common.dates import first_of_month, fmt_date, parse_date, pay_month_label
from common.settings import get_settings_dict
from payroll.calculator import held_balance
from payroll.sheet_import import SHEET_LABELS

def render(self, conn, is_approver, pending_count, pm_str, role, ym, body=""):
    run = conn.execute("SELECT * FROM payroll_runs WHERE payroll_month = ?", (pm_str,)).fetchone()
    stage = run["status"] if run else "Draft"

    def stage_btn(path, label, css, roles):
        if role not in roles:
            return ""
        return f"<form method='POST' action='{path}?ym={ym}'><button class='btn {css}' type='submit'>{label}</button></form>"

    def signed(who, when):
        return f"<b>{html_lib.escape(who or '')}</b> on {(when or '').replace('T', ' ')}" if who else "—"

    steps = (f"Prepared: {signed(run['prepared_by'], run['prepared_date'])} &nbsp;|&nbsp; "
             f"Reviewed: {signed(run['reviewed_by'], run['reviewed_date'])} &nbsp;|&nbsp; "
             f"Approved: {signed(run['approved_by'], run['approved_date'])}") if run else ""
    pending_note = f" <b>{pending_count} change request(s) are still pending.</b>" if pending_count else ""
    unreviewed = conn.execute("SELECT COUNT(*), COALESCE(SUM(total_amount), 0) FROM travel_claims WHERE payroll_month = ? AND status = 'Submitted'",
                              (pm_str,)).fetchone()
    if unreviewed[0]:
        pending_note += (f" <b>{unreviewed[0]} travel claim(s) (Rs. {unreviewed[1]:,.0f}) still await the reviewer</b> — they are not paid "
                         "this month unless reviewed before the payroll is prepared (otherwise they are paid next month).")
    send_back = stage_btn("/send_back_month", "Send Back to Draft", "btn-danger",
                          (ROLE_APPROVER,) if stage == "Approved" else (ROLE_APPROVER, ROLE_REVIEWER))
    if stage == "Draft":
        run_text = f"<span class='badge b-warn'>Draft</span> Figures can still change.{pending_note}"
        run_btns = stage_btn("/prepare_month", "Mark as Prepared", "btn-ok", ROLE_ACTIONS["/prepare_month"])
    elif stage == "Prepared":
        run_text = "<span class='badge b-warn'>Prepared — awaiting review</span> Figures are locked."
        run_btns = stage_btn("/review_month", "Mark as Reviewed", "btn-ok", ROLE_ACTIONS["/review_month"]) + send_back
    elif stage == "Reviewed":
        run_text = f"<span class='badge b-warn'>Reviewed — awaiting approval</span> Figures are locked.{pending_note}"
        run_btns = stage_btn("/approve_month", f"Approve {pay_month_label(pm_str)} Payroll", "btn-ok", ROLE_ACTIONS["/approve_month"]) + send_back
    else:
        run_text = "<span class='badge b-ok'>Approved</span> Figures are locked and bank files can be exported."
        run_btns = send_back
    template_btn = f"<a class='btn btn-sec' href='/payroll_template?ym={ym}'>Download Payroll Sheet Template (Excel)</a>"
    runbar = f"""<div class='panel runbar'>
              <div>{pay_month_label(pm_str)} payroll: {run_text}<div class='note' style='margin:6px 0 0 0;'>{steps}</div></div>
              <div style='display:flex; gap:8px; flex-wrap:wrap;'>{template_btn}{run_btns}</div></div>"""
    lines = conn.execute("SELECT * FROM payroll_lines WHERE payroll_month = ? ORDER BY emp_id", (pm_str,)).fetchall()
    tot_gross = sum(r["gross_earned"] for r in lines)
    tot_net = sum(r["net_pay"] for r in lines)
    tot_eobi = sum(r["eobi_cost_accrued"] for r in lines)
    tot_cost = sum(r["total_cost_to_firm"] for r in lines)
    tot_loan = sum(r["loan_advance_recovery"] for r in lines)
    tot_ta = sum(r["travelling_allowance"] for r in lines)

    cards = f"""
            <div class="cards">
              <div class="card"><h3>Active Headcount</h3><div class="val">{sum(r['on_payroll'] for r in lines)}</div><div class="sub">{sum(r['on_probation'] for r in lines)} with pay on hold</div></div>
              <div class="card"><h3>Gross Salary Earned</h3><div class="val">Rs. {tot_gross:,.0f}</div><div class="sub">TA + Bonus: Rs. {tot_ta:,.0f}</div></div>
              <div class="card"><h3>Loan / Adv Recovered</h3><div class="val">Rs. {tot_loan:,.0f}</div><div class="sub">Tax Deducted: Rs. 0</div></div>
              <div class="card"><h3>EOBI Cost (6% Firm)</h3><div class="val">Rs. {tot_eobi:,.0f}</div><div class="sub">5% Er + 1% Ee borne by firm · {sum(1 for r in lines if r['payment_mode'] == 'EOBI only' and r['eobi_cost_accrued'])} EOBI-only (non-employees)</div></div>
              <div class="card" style="border-left-color:#16a34a;"><h3>Net Pay to Transfer</h3><div class="val">Rs. {tot_net:,.0f}</div><div class="sub">Total Cost: Rs. {tot_cost:,.0f}</div></div>
            </div>
            """
    rows_html = ""
    for r in lines:
        chk_badge = f"<span class='badge b-warn'>{html_lib.escape(r['validation_checks'])}</span>" if r["validation_checks"] else "<span class='badge b-ok'>OK</span>"
        if r["leave_balance"] is None:
            lv_bal = "—"
        else:
            lv_bal = f"<span class='badge {'b-ok' if r['leave_flag'] == 'OK' else 'b-warn'}'>{r['leave_balance']:g} / {r['leave_limit']:g}</span>"
        rows_html += f"""
                <tr>
                  <td>{r['emp_id']}</td>
                  <td><strong>{html_lib.escape(r['employee_name'] or '')}</strong></td>
                  <td>{html_lib.escape(r['staff_type']) if r['staff_type'] else '<em>(unset)</em>'}</td>
                  <td class="num">{r['gross_earned']:,.0f}</td>
                  <td class="num">{r['travelling_allowance']:,.0f}</td>
                  <td class="num">{r['probation_salary_released']:,.0f}</td>
                  <td class="num">{f"<span class='badge b-ok'>{r['leave_encashment']:,.0f}</span><br><span class='note'>{r['leave_encash_days']:g} days</span>" if r['leave_encashment'] else 0}</td>
                  <td class="num">{r['leaves_this_month']:g}</td>
                  <td class="num">{r['leaves_cumulative']:g}</td>
                  <td class="num">{lv_bal}</td>
                  <td class="num">{r['leave_deduction']:,.0f}</td>
                  <td class="num">{r['loan_advance_recovery']:,.0f}</td>
                  <td class="num">{r['salary_withheld_probation']:,.0f}</td>
                  <td class="num">{f"<span class='badge b-warn'>{r['salary_withheld_absence']:,.0f}</span>" if r['salary_withheld_absence'] else 0}{f"<br><span class='note'>+{r['absence_salary_released']:,.0f} released</span>" if r['absence_salary_released'] else ''}</td>
                  <td class="num">{f"<span class='badge b-warn'>{r['salary_withheld_late']:,.0f}</span>" if r['salary_withheld_late'] else 0}{f"<br><span class='note'>+{r['late_salary_released']:,.0f} released</span>" if r['late_salary_released'] else ''}</td>
                  <td class="num">{f"<span class='badge b-warn'>{r['salary_withheld_timesheet']:,.0f}</span>" if r['salary_withheld_timesheet'] else 0}{f"<br><span class='note'>+{r['timesheet_salary_released']:,.0f} released</span>" if r['timesheet_salary_released'] else ''}</td>
                  <td class="num"><strong>{r['net_pay']:,.0f}</strong></td>
                  <td class="num">{r['eobi_cost_accrued']:,.0f}</td>
                  <td>{r['payment_mode']}</td>
                  <td>{chk_badge}</td>
                </tr>"""
    holds = conn.execute("""
                SELECT p.emp_id, p.employee_name, p.payroll_month, p.salary_withheld_absence AS held, run.status,
                       COALESCE((SELECT SUM(amount) FROM salary_releases r
                                 WHERE r.emp_id = p.emp_id AND r.withheld_month = p.payroll_month), 0) AS released
                FROM payroll_lines p JOIN payroll_runs run ON run.payroll_month = p.payroll_month
                WHERE p.salary_withheld_absence > 0 AND (run.status = 'Approved' OR p.payroll_month = ?)
                ORDER BY p.payroll_month, p.emp_id
            """, (pm_str,)).fetchall()
    can_release_here = is_approver and stage == "Draft"

    def hold_action(h):
        outstanding = h["held"] - h["released"]
        if outstanding <= 0:
            return "<span class='badge b-ok'>Released</span>"
        if h["status"] != "Approved":
            return "Held — can be released once this month is approved"
        if not can_release_here or h["payroll_month"] >= pm_str:
            return "Held — the approver can release it into a later Draft month"
        return (f"<form method='POST' action='/release_salary?ym={ym}'><input type='hidden' name='emp_id' value='{h['emp_id']}'>"
                f"<input type='hidden' name='withheld_month' value='{h['payroll_month']}'>"
                f"<button class='btn btn-ok' type='submit'>Release Rs. {outstanding:,.0f} in {pay_month_label(pm_str)}</button></form>")

    hold_rows = "".join(f"""
                <tr><td>{h['emp_id']}</td><td><strong>{html_lib.escape(h['employee_name'] or '')}</strong></td><td>{pay_month_label(h['payroll_month'])}</td>
                  <td class="num">{h['held']:,.0f}</td><td class="num">{h['released']:,.0f}</td><td>{hold_action(h)}</td></tr>""" for h in holds)
    holds_panel = f"""
            <div class="panel">
              <h3>Withheld Salaries (Unapproved Absence)</h3>
              <p class="note">A recorded unapproved absence withholds the employee's whole net salary for that month. It stays withheld
                until the approver releases it into a later payroll month.</p>
              <table>
                <thead><tr><th>ID</th><th>Employee</th><th>Withheld In</th><th class="num">Withheld</th><th class="num">Released</th><th></th></tr></thead>
                <tbody>{hold_rows}</tbody>
              </table>
            </div>""" if holds else ""

    late_holds = conn.execute("""
                SELECT p.emp_id, p.employee_name, p.payroll_month, p.salary_withheld_late AS held, run.status,
                       r.release_month, r.released_by
                FROM payroll_lines p JOIN payroll_runs run ON run.payroll_month = p.payroll_month
                LEFT JOIN late_hold_releases r ON r.emp_id = p.emp_id AND r.withheld_month = p.payroll_month
                WHERE p.salary_withheld_late > 0 AND (run.status = 'Approved' OR p.payroll_month = ?)
                ORDER BY p.payroll_month, p.emp_id
            """, (pm_str,)).fetchall()
    late_cfg = get_settings_dict(conn)

    def late_action(h):
        if h["release_month"]:
            return f"<span class='badge b-ok'>Released in {pay_month_label(h['release_month'])}</span>"
        if not is_approver:
            return "Held — awaiting the approver's release"
        if stage != "Draft":
            return "Held — the approver can release it into a Draft month"
        if h["payroll_month"] > pm_str or (h["payroll_month"] < pm_str and h["status"] != "Approved"):
            return "Held"
        return (f"<form method='POST' action='/release_late_hold?ym={ym}' style='display:flex; gap:6px; flex-wrap:wrap; align-items:center;'>"
                f"<input type='hidden' name='emp_id' value='{h['emp_id']}'><input type='hidden' name='withheld_month' value='{h['payroll_month']}'>"
                "<input name='note' size='18' placeholder='Reason (optional)'>"
                f"<button class='btn btn-ok' type='submit'>Release Rs. {h['held']:,.0f} in {pay_month_label(pm_str)}</button></form>")

    def late_dates(h):
        return late_days_text(late_arrival_days(conn, h["emp_id"], h["payroll_month"], late_cfg))

    late_rows = "".join(f"""
                <tr><td>{h['emp_id']}</td><td><strong>{html_lib.escape(h['employee_name'])}</strong></td><td>{pay_month_label(h['payroll_month'])}</td>
                  <td>{late_dates(h)}</td><td class="num">{h['held']:,.0f}</td><td>{late_action(h)}</td></tr>""" for h in late_holds)
    late_panel = f"""
            <div class="panel">
              <h3>Salaries Held for Late Arrivals</h3>
              <p class="note">Late arrivals are never deducted. On {late_cfg.get('late_hold_days', '3')} late arrivals in a calendar month (more than {late_cfg.get('late_grace_minutes', '15')} minutes
                after the person's start time), that month's salary or stipend is held, and only the approver can release it.</p>
              <table>
                <thead><tr><th>ID</th><th>Employee</th><th>Held In</th><th>Late On</th><th class="num">Held (Rs.)</th><th></th></tr></thead>
                <tbody>{late_rows}</tbody>
              </table>
            </div>""" if late_holds else ""

    held_rows = ""
    for s in conn.execute("SELECT * FROM staff WHERE COALESCE(staff_type, '') NOT IN ('Permanent', ?) ORDER BY emp_id", (EOBI_ONLY_TYPE,)).fetchall():
        bal = held_balance(conn, s, pm_str)
        if bal <= 0:
            continue
        leaving = s["status"] == "Left" or s["leaving_date"]
        on_hold = next((l["on_probation"] for l in lines if l["emp_id"] == s["emp_id"]), 0)
        if s["staff_type"] in ARTICLE_TYPES:
            why = "Stipend held until the ICAP registration card is received"
        elif on_hold:
            why = "Pay held until 4 months are complete"
        else:
            why = "One month's pay held as security until exit"
        action = ""
        left_on = parse_date(s["leaving_date"])
        if is_approver and stage == "Draft" and leaving and not (left_on and fmt_date(first_of_month(left_on)) > pm_str):
            action = (f"<form method='POST' action='/settle_security?ym={ym}' style='display:flex; gap:6px; flex-wrap:wrap; align-items:center;'>"
                      f"<input type='hidden' name='emp_id' value='{s['emp_id']}'>"
                      "<label style='flex-direction:row; gap:4px; align-items:center;'><input type='checkbox' name='handover' value='1'> Notice served, handover done</label>"
                      "<button class='btn btn-ok' type='submit' name='decision' value='Released'>Release</button>"
                      "<input name='note' size='16' placeholder='Reason to forfeit'>"
                      "<button class='btn btn-danger' type='submit' name='decision' value='Forfeited'>Forfeit</button></form>")
        elif leaving:
            action = "<span class='note'>The approver settles this in a Draft month</span>"
        held_rows += (f"<tr><td>{s['emp_id']}</td><td><strong>{html_lib.escape(s['employee_name'])}</strong></td><td>{html_lib.escape(s['staff_type'] or '')}</td>"
                      f"<td>{html_lib.escape(s['status'] or '')}{' — leaving ' + html_lib.escape(s['leaving_date']) if s['leaving_date'] else ''}</td><td>{why}</td>"
                      f"<td class='num'><strong>{bal:,.0f}</strong></td><td>{action}</td></tr>")
    held_panel = f"""
            <div class="panel">
              <h3>Held Pay &amp; Security</h3>
              <p class="note"><b>Internees:</b> pay is released after 4 months, keeping one month as security until they leave after proper notice and
                handing / taking over. <b>ICAP / NOC students:</b> stipend builds up until the ICAP registration card is received; no security.
                <b>Permanent staff:</b> regular pay.</p>
              <table><thead><tr><th>ID</th><th>Name</th><th>Type</th><th>Status</th><th>Why Held</th><th class="num">Held (Rs.)</th><th></th></tr></thead>
                <tbody>{held_rows}</tbody></table>
            </div>""" if held_rows else ""

    import_panel = ""
    if is_approver:
        esc = html_lib.escape
        preview = conn.execute("SELECT * FROM payroll_imports WHERE payroll_month = ? AND status = 'Preview' ORDER BY id DESC LIMIT 1",
                               (pm_str,)).fetchone()
        applied_from = run["imported_from"] if run else None
        preview_html = ""
        if preview:
            rows = json.loads(preview["rows"])
            mapping = json.loads(preview["mapping"])
            unmatched = json.loads(preview["unmatched"])
            sys_lines = {l["emp_id"]: l for l in lines}
            missing = [l for l in lines if l["on_payroll"] and l["emp_id"] not in rows]
            diff_rows = ""
            for emp, vals in sorted(rows.items()):
                sl = sys_lines.get(emp)
                sheet_net = vals.get("net_pay") or 0
                sys_net = sl["net_pay"] if sl else 0
                name = sl["employee_name"] if sl else emp
                flag = "" if abs(sheet_net - sys_net) < 1 else f"<span class='badge b-warn'>{sheet_net - sys_net:+,.0f}</span>"
                diff_rows += (f"<tr><td>{emp}</td><td>{esc(name)}</td><td class='num'>{sys_net:,.0f}</td>"
                              f"<td class='num'><b>{sheet_net:,.0f}</b></td><td class='num'>{flag}</td></tr>")
            sheet_total = sum((v.get("net_pay") or 0) for v in rows.values())
            preview_html = f"""
              <div style="border:1px solid #f59e0b; border-radius:8px; padding:12px; margin-top:12px;">
                <h4 style="margin:0 0 6px;">Preview — {esc(preview['file_name'])} (sheet “{esc(preview['sheet_name'])}”), uploaded {preview['uploaded_at'][:16].replace('T', ' ')}</h4>
                <p class="note">Columns recognised: {esc(', '.join(f'{h} → {SHEET_LABELS.get(f, f)}' for h, f in mapping.items()))}.<br>
                  <b>{len(rows)}</b> staff matched; sheet net pay <b>Rs. {sheet_total:,.0f}</b> (system now Rs. {tot_net:,.0f}).
                  {f"<br><span class='badge b-warn'>Not matched to any staff member:</span> {esc(', '.join(unmatched))}" if unmatched else ""}
                  {f"<br><span class='badge b-warn'>On the system payroll but not on the sheet (will be paid nothing):</span> {esc(', '.join(l['emp_id'] + ' ' + l['employee_name'] for l in missing))}" if missing else ""}</p>
                <details><summary style="cursor:pointer;">Net pay, system vs sheet</summary>
                  <table style="margin-top:6px;"><thead><tr><th>ID</th><th>Name</th><th class="num">System</th><th class="num">Sheet</th><th class="num">Difference</th></tr></thead>
                  <tbody>{diff_rows}</tbody></table></details>
                <div style="display:flex; gap:10px; margin-top:10px; flex-wrap:wrap; align-items:center;">
                  <form method="POST" action="/import_payroll_apply?ym={ym}" style="display:flex; gap:10px; align-items:center;">
                    <input type="hidden" name="id" value="{preview['id']}">
                    <label style="flex-direction:row; gap:4px; align-items:center;"><input type="checkbox" name="update_salaries" value="1"> Also update staff monthly salaries from the sheet</label>
                    <button class="btn btn-ok" type="submit">Apply as Final {pay_month_label(pm_str)} Payroll</button>
                  </form>
                  <form method="POST" action="/import_payroll_discard?ym={ym}"><input type="hidden" name="id" value="{preview['id']}">
                    <button class="btn btn-sec" type="submit">Discard</button></form>
                </div>
              </div>"""
        import_panel = f"""
            <div class="panel">
              <h3>Upload Final Payroll Sheet (Excel) — {pay_month_label(pm_str)}</h3>
              <p class="note">For a month finalised outside the system (e.g. November 2026 by the reviewer). The sheet's columns are recognised by
                their headings (Emp ID or Name, and Net Pay; also Gross, Travel, Leave Ded., Loan, Prob. Withheld, EOBI … when present).
                You see a preview first; applying it replaces this month's figures with the sheet's, and approves and locks the month.
                Later months are calculated by the system's own rules.
                {f"<br><b>This month's figures come from the final sheet “{esc(applied_from)}”.</b> Upload a corrected sheet to replace them." if applied_from else ""}</p>
              <form class="inline" method="POST" action="/import_payroll_upload?ym={ym}" enctype="multipart/form-data">
                <label>Final Payroll Sheet <input type="file" name="sheet" accept=".xlsx,.xlsm,.csv" required></label>
                <button class="btn" type="submit">Upload &amp; Preview</button>
              </form>
              {preview_html}
            </div>"""
    body = runbar + import_panel + cards + late_panel + held_panel + holds_panel + f"""
            <div class="panel">
              <table>
                <thead><tr>
                  <th>ID</th><th>Employee</th><th>Type</th><th class="num">Gross</th><th class="num">Travel</th>
                  <th class="num">Prob. Rel.</th><th class="num">Leave Encash.</th><th class="num">Leaves (Mo.)</th><th class="num">Leaves (Cum.)</th><th class="num">Leave Bal. / Limit</th><th class="num">Leave Ded.</th><th class="num">Loan Rec.</th>
                  <th class="num">Prob. Withheld</th><th class="num">Absence Hold</th><th class="num">Late Hold</th><th class="num">Timesheet Hold</th><th class="num">Net Pay</th><th class="num">EOBI (Firm)</th><th>Mode</th><th>Checks</th>
                </tr></thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>"""
    return body
