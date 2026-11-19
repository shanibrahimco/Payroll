"""The leaves tab of the web app."""

from datetime import date
import html as html_lib
import json

from auth.roles import ROLE_ACTIONS, ROLE_APPROVER, ROLE_PREPARER, ROLE_REVIEWER
from common.constants import ABSENCE_LEAVE_TYPE, CREDIT_REASONS, EOBI_ONLY_TYPE, LEAVE_TYPES, NOC_TYPE, STUDY_LEAVE_TYPE, URGENT_WORK_ABROAD
from common.dates import parse_date, pay_month_label
from common.messages import FLASH_MESSAGES
from common.settings import get_settings_dict, is_month_locked
from leaves.history_import import LEAVE_HISTORY_FROM
from leaves.rules import credit_review_form

def render(self, approver_only, cannot_decide, conn, is_approver, is_staff, own_emp, pm_str, role, ym, body=""):
    only_emp = (own_emp or "") if is_staff else None  # Staff see only their own records
    lvs = conn.execute("""SELECT l.*, s.employee_name FROM leave_register l JOIN staff s ON l.emp_id=s.emp_id
                                  WHERE ? IS NULL OR l.emp_id = ? ORDER BY l.id DESC""", (only_emp, only_emp)).fetchall()

    reviewer_emps = approver_only

    def leave_buttons(l):
        """Only the steps decide_leave() would accept for this user."""
        if (role not in ROLE_ACTIONS["/decide_leave"] or is_month_locked(conn, l["payroll_month"])
                or (cannot_decide(l["emp_id"]) and not (l["status"] == "Recorded" and l["emp_id"] != own_emp))):
            return []
        st = l["status"]
        steps = []
        if role == ROLE_REVIEWER and st == "Pending":
            steps.append(("Recommended", "Recommend", "btn-ok"))
        if role == ROLE_APPROVER and (st == "Recommended" or (st == "Pending" and l["emp_id"] in reviewer_emps)):
            steps.append(("Approved", "Approve", "btn-ok"))
        if st in ("Pending", "Recommended"):
            steps.append(("Rejected", "Reject", "btn-danger"))
        if st == "Recorded":
            steps.append(("Cancelled", "Cancel Record", "btn-danger"))
        return steps

    def leave_action(l):
        return "<div style='display:flex; gap:6px;'>" + "".join(
            f"<form method='POST' action='/decide_leave?ym={ym}'><input type='hidden' name='id' value='{l['id']}'>"
            f"<input type='hidden' name='decision' value='{d}'><button class='btn {css}' type='submit'>{label}</button></form>"
            for d, label, css in leave_buttons(l)) + "</div>"

    def leave_status_cell(l):
        st = l["status"]
        who = l["approved_by"] if st in ("Approved", "Rejected", "Cancelled") else l["recommended_by"] if st == "Recommended" else None
        css = "b-ok" if st == "Approved" else "b-warn"
        rec = f"<br><span class='note'>Recommended by {html_lib.escape(l['recommended_by'])}</span>" if l["recommended_by"] and st != "Recommended" else ""
        return f"<span class='badge {css}'>{st}</span>{f' by {html_lib.escape(who)}' if who else ''}{rec}"

    rows_html = "".join(f"""
                <tr>
                  <td>{l['application_date']}</td><td>{l['emp_id']}</td><td>{l['employee_name']}</td>
                  <td>{l['leave_type']}</td><td>{l['date_from']}</td><td>{l['date_to']}</td>
                  <td class="num">{l['no_of_days']}</td><td>{leave_status_cell(l)}</td><td>{l['payroll_month']}</td><td>{html_lib.escape(l['reason'] or '')}{f"<br><span class='note'>filed by {html_lib.escape(l['entered_by'])}</span>" if l['entered_by'] and ' for ' in l['entered_by'] else ''}</td>
                  <td>{leave_action(l)}</td>
                </tr>""" for l in lvs)

    bulk = ""
    if role == ROLE_REVIEWER:
        n = sum(1 for l in lvs if ("Recommended", "Recommend", "btn-ok") in leave_buttons(l))
        if n:
            bulk = f"<form method='POST' action='/bulk_leave?ym={ym}'><input type='hidden' name='decision' value='Recommended'><button class='btn btn-ok' type='submit'>Recommend All Pending ({n})</button></form>"
    elif role == ROLE_APPROVER:
        n = sum(1 for l in lvs if ("Approved", "Approve", "btn-ok") in leave_buttons(l))
        if n:
            bulk = f"<form method='POST' action='/bulk_leave?ym={ym}'><input type='hidden' name='decision' value='Approved'><button class='btn btn-ok' type='submit'>Approve All Recommended ({n})</button></form>"

    opening_as_at = parse_date(get_settings_dict(conn).get("opening_balances_as_at"))
    summary = conn.execute("""
                SELECT p.*, s.opening_leaves_cumulative FROM payroll_lines p JOIN staff s ON s.emp_id = p.emp_id
                WHERE p.payroll_month = ? AND (? IS NULL OR p.emp_id = ?) AND COALESCE(p.staff_type, '') != ?
                ORDER BY p.emp_id
            """, (pm_str, only_emp, only_emp, EOBI_ONLY_TYPE)).fetchall()

    enters_for_others = role in (ROLE_APPROVER, ROLE_PREPARER)
    chain_note = ("Every leave is recorded as <b>Pending</b>, recommended by the reviewer, then approved by the approver. "
                  "Nobody can decide their own leave.")
    if enters_for_others:
        leave_who = '<label>Emp ID <input name="emp_id" placeholder="E001" required size="6"></label>'
        leave_types = LEAVE_TYPES
        leave_note = (f"{chain_note} An <b>{ABSENCE_LEAVE_TYPE}</b> is recorded straight away and "
                      "<b>withholds that month's whole salary</b> until the approver releases it.")
    else:
        leave_who = f'<input type="hidden" name="emp_id" value="{own_emp or ""}">'
        leave_types = tuple(t for t in LEAVE_TYPES if t != ABSENCE_LEAVE_TYPE)
        leave_note = (f"You can apply for your own leave only. {chain_note}" if own_emp else FLASH_MESSAGES["no_emp_link"][1])
    type_options = "".join(f"<option>{t}</option>" for t in leave_types)

    def summary_row(r):
        opening = float(r["opening_leaves_cumulative"] or 0)
        if r["leave_balance"] is None:
            limit_cell, bal_cell = "No limit set", "—"
        else:
            basis = {"ICAP Article": "whole articleship", NOC_TYPE: "residual leaves"}.get(r["staff_type"], "per year")
            limit_cell = f"{r['leave_limit']:g} ({basis})"
            bal_cell = f"<span class='badge {'b-ok' if r['leave_flag'] == 'OK' else 'b-warn'}'>{r['leave_balance']:g} {r['leave_flag']}</span>"
        return f"""
                <tr>
                  <td>{r['emp_id']}</td><td><strong>{r['employee_name']}</strong></td><td>{r['staff_type'] or ''}</td>
                  <td class="num">{opening:g}</td><td class="num">{r['leaves_cumulative'] - opening:g}</td>
                  <td class="num">{r['leaves_this_month']:g}</td><td class="num">{r['leaves_this_year']:g}</td>
                  <td class="num"><strong>{r['leaves_cumulative']:g}</strong></td>
                  <td>{limit_cell}</td><td class="num">{r['leave_carried_forward']:g}</td><td class="num">{r['leave_credit_days']:g}</td><td class="num">{bal_cell}</td>
                  <td class="num">{r['comp_leave_used']:g}</td>
                  <td class="num">{r['excess_leave_days']:g}</td><td class="num">{r['leave_deduction']:,.0f}</td>
                </tr>"""

    leave_import_panel = ""
    if is_approver:
        esc = html_lib.escape
        imp = conn.execute("SELECT * FROM leave_imports WHERE status = 'Preview' ORDER BY id DESC LIMIT 1").fetchone()
        preview = ""
        if imp:
            d = json.loads(imp["data"])
            names = {s["emp_id"]: s["employee_name"] for s in conn.execute("SELECT emp_id, employee_name FROM staff")}
            per = {}
            for l in d["leaves"]:
                p = per.setdefault(l["emp_id"], {"n": 0, "days": 0.0, "study": 0.0})
                p["n"] += 1
                p["days"] += l["days"]
                p["study"] += l["days"] if l["leave_type"] == STUDY_LEAVE_TYPE else 0
            emps = sorted(set(per) | set(d["opening"]))
            existing = {r[0]: r[1] for r in conn.execute(
                "SELECT emp_id, COUNT(*) FROM leave_register WHERE date_from >= ? AND status != 'Recorded' GROUP BY emp_id", (LEAVE_HISTORY_FROM,))}
            def preview_row(e):
                if e in d["opening"]:
                    days, study = d["opening"][e]
                    opening = f"{days:g}" + (f" ({study:g} study)" if study else "")
                else:
                    opening = "—"
                p = per.get(e, {"n": 0, "days": 0, "study": 0})
                return (f"<tr><td>{e}</td><td>{esc(names.get(e, ''))}</td><td class='num'>{opening}</td>"
                        f"<td class='num'>{p['n']}</td><td class='num'>{p['days']:g}</td><td class='num'>{p['study']:g}</td>"
                        f"<td class='num'>{existing.get(e, 0)}</td></tr>")

            rows = "".join(preview_row(e) for e in emps)
            problems = "".join(f"<li>{esc(p)}</li>" for p in d["problems"])
            preview = f"""
              <div style="border:1px solid #f59e0b; border-radius:8px; padding:12px; margin-top:10px;">
                <h4 style="margin:0 0 6px;">Preview — {esc(imp['file_name'])}</h4>
                <p class="note">Opening balances for <b>{len(d['opening'])}</b> staff (become “as at 31 Dec 2025”); <b>{len(d['leaves'])}</b> leave records from 1 Jan 2026,
                  recorded as <b>approved</b> by you.</p>
                {f"<div class='flash flash-warn'>Rows that will be skipped:<ul style='margin:4px 0;'>{problems}</ul></div>" if problems else ""}
                <table><thead><tr><th>ID</th><th>Name</th><th class="num">Before 1 Jan 2026</th><th class="num">Leaves 2026 (rows)</th>
                  <th class="num">Days</th><th class="num">Of which Study</th><th class="num">2026 records already in the system</th></tr></thead><tbody>{rows}</tbody></table>
                <form method="POST" action="/leave_import_apply?ym={ym}" style="display:flex; gap:10px; align-items:center; margin-top:10px; flex-wrap:wrap;">
                  <input type="hidden" name="id" value="{imp['id']}">
                  <label style="flex-direction:row; gap:4px; align-items:center;"><input type="checkbox" name="replace" value="1" checked>
                    Replace the 2026 leave records already in the system for these staff (recorded absences are kept)</label>
                  <button class="btn btn-ok" type="submit">Apply Leave History</button>
                </form>
                <form method="POST" action="/leave_import_discard?ym={ym}" style="margin-top:6px;"><input type="hidden" name="id" value="{imp['id']}">
                  <button class="btn btn-sec" type="submit">Discard</button></form>
              </div>"""
        leave_import_panel = f"""
            <div class="panel">
              <h3>Upload Leave History</h3>
              <p class="note">Opening balances as at 31 December 2025 plus every leave from 1 January 2026 (study leave as “Exam / Study”).
                <a href="/leave_template?ym={ym}">Download the Leave History template</a>, fill it in, and upload it here. You see a preview first.</p>
              <form class="inline" method="POST" action="/leave_import_upload?ym={ym}" enctype="multipart/form-data">
                <label>Leave History (Excel) <input type="file" name="leave_file" accept=".xlsx" required></label>
                <button class="btn" type="submit">Upload &amp; Preview</button>
              </form>
              {preview}
            </div>"""
    body = leave_import_panel + f"""
            <div class="panel">
              <h3>Leave Summary — {pay_month_label(pm_str)}</h3>
              <p style="font-size:12.5px; color:#64748b; margin:0 0 12px 0;">
                Leave rules: <b>Permanent &amp; Internees</b> — 24 leaves per calendar year; on 1 January unused leave is carried
                forward (leave bank up to 40 days) and days above 40 are paid in January's payroll &nbsp;|&nbsp;
                <b>ICAP Articleship</b> — 130 leaves over the 3 years 6 months tenure &nbsp;|&nbsp;
                <b>NOC Articleship</b> — only the residual leaves, over the residual period &nbsp;|&nbsp;
                Everyone gets <b>2 paid leaves per month</b>; days beyond that are deducted from salary.
                <b>Study leave</b> ("{STUDY_LEAVE_TYPE}") and <b>{URGENT_WORK_ABROAD}</b> are never deducted, but still count towards the balance.
              </p>
              <table>
                <thead><tr><th>ID</th><th>Name</th><th>Type</th>
                  <th class="num">Opening ({opening_as_at.strftime('%d %b %Y') if opening_as_at else 'b/f'})</th>
                  <th class="num">Taken Since</th><th class="num">This Month</th><th class="num">This Year</th>
                  <th class="num">Cumulative</th><th>Entitlement</th><th class="num">B/F (incl.)</th><th class="num">Credits (incl.)</th><th class="num">Balance</th>
                  <th class="num">Credit Used (Mo.)</th><th class="num">Days Deducted</th><th class="num">Deduction (Rs.)</th></tr></thead>
                <tbody>{"".join(summary_row(r) for r in summary)}</tbody>
              </table>
            </div>
            <div class="panel">
              <h3>{'Add Leave Application' if enters_for_others else 'Apply for Leave'} (Auto-applies 25th cut-off rule)</h3>
              <p class="note">{leave_note}</p>
              <form class="inline" method="POST" action="/add_leave?ym={ym}">
                <label>App Date <input type="date" name="application_date" value="{date.today().isoformat()}" required></label>
                {leave_who}
                <label>Type <select name="leave_type">{type_options}</select></label>
                <label>From <input type="date" name="date_from" required></label>
                <label>To <input type="date" name="date_to" required></label>
                <label>Days <input type="number" step="0.5" name="no_of_days" value="1" required style="width:70px;"></label>
                <label>Reason <input name="reason" placeholder="Reason"></label>
                <button class="btn" type="submit">{'Add Leave' if enters_for_others else 'Apply'}</button>
              </form>
            </div>
            <div class="panel">
              <div class="runbar" style="margin-bottom:12px;"><h3 style="margin:0;">Leave Applications</h3>{bulk}</div>
              <table>
                <thead><tr><th>App Date</th><th>ID</th><th>Name</th><th>Type</th><th>From</th><th>To</th><th class="num">Days</th><th>Status</th><th>Payroll Month</th><th>Reason</th><th></th></tr></thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>"""

    esc = html_lib.escape

    # Leave credits: extra leave for holiday / weekend work, recommended by the reviewer, approved by the approver
    credits = conn.execute("""SELECT c.*, s.employee_name, s.staff_type FROM leave_credits c JOIN staff s ON s.emp_id = c.emp_id
                                      WHERE ? IS NULL OR c.emp_id = ? ORDER BY c.id DESC LIMIT 200""", (only_emp, only_emp)).fetchall()

    def credit_status(c):
        label = {"Applied": "With reviewer", "Late": "Late: needs condonation"}.get(c["status"], c["status"])
        css = "b-ok" if c["status"] == "Approved" else "b-warn"
        condoned = f"<br><span class='note'>Delay condoned by {esc(c['condoned_by'])}: {esc(c['condone_note'] or '')}</span>" if c["condoned_by"] else ""
        return f"<span class='badge {css}'>{label}</span>{f' by {esc(c['decided_by'])}' if c['decided_by'] else ''}{condoned}"

    def credit_evidence(c):
        if is_staff:
            return ""
        a = conn.execute("SELECT status, arrival_time FROM attendance WHERE emp_id = ? AND att_date = ?", (c["emp_id"], c["work_date"])).fetchone()
        text = f"{a['status']}{' at ' + a['arrival_time'] if a['arrival_time'] else ''}" if a else "no record"
        return f"<br><span class='note'>Attendance: {esc(text)}</span>"

    def credit_action(c):
        if c["status"] in ("Applied", "Late"):
            late = c["status"] == "Late"
            if c["emp_id"] in approver_only:
                return credit_review_form(c, ym, "Condone &amp; Approve" if late else "Approve") if role == ROLE_APPROVER else "<span class='note'>Awaiting approver</span>"
            return credit_review_form(c, ym, "Condone &amp; Recommend" if late else "Recommend") if role == ROLE_REVIEWER else "<span class='note'>Awaiting reviewer</span>"
        if c["status"] != "Recommended":
            return ""
        if role != ROLE_APPROVER:
            return "<span class='note'>Awaiting approver</span>"
        return "<div style='display:flex; gap:6px;'>" + "".join(
            f"<form method='POST' action='/decide_credit?ym={ym}'><input type='hidden' name='id' value='{c['id']}'>"
            f"<input type='hidden' name='decision' value='{d}'><button class='btn {css}' type='submit'>{label}</button></form>"
            for d, label, css in (("Approved", "Approve", "btn-ok"), ("Rejected", "Reject", "btn-danger"))) + "</div>"

    credit_rows = "".join(f"""
                <tr><td>{c['emp_id']}</td><td>{esc(c['employee_name'])}</td><td>{parse_date(c['work_date']).strftime('%a %d %b %Y')}</td>
                  <td class="num">{c['days']:g}</td><td>{CREDIT_REASONS.get(c['reason'], c['reason'])}{f"<br><span class='note'>{esc(c['note'])}</span>" if c['note'] else ''}{credit_evidence(c)}</td>
                  <td>{c['source']}</td><td>{esc(c['requested_by'] or '')}</td>
                  <td>{credit_status(c)}</td>
                  <td>{credit_action(c)}</td></tr>""" for c in credits)
    credit_days = int(float(get_settings_dict(conn).get("credit_apply_days", 7)))
    me_row = conn.execute("SELECT staff_type, status FROM staff WHERE emp_id = ?", (own_emp,)).fetchone() if own_emp else None
    apply_credit_form = ""
    if me_row is not None and me_row["status"] == "Active" and me_row["staff_type"] != EOBI_ONLY_TYPE:
        apply_credit_form = f"""
              <h4 style="margin:12px 0 6px;">Apply for Credit Leave</h4>
              <form class="inline" method="POST" action="/apply_credit?ym={ym}">
                <label>Date Worked <input type="date" name="work_date" required max="{date.today().isoformat()}"></label>
                <label>Work Done <input name="note" required size="30" placeholder="e.g. audit closing at client"></label>
                <label style="flex-direction:row; align-items:center; gap:4px;"><input type="checkbox" name="full_day" value="1" required> I worked a complete working day</label>
                <button class="btn" type="submit">Submit Application</button>
              </form>"""
    credit_form = ""
    if role in ROLE_ACTIONS["/add_credit"]:
        reason_opts = "".join(f"<option value='{k}'>{v}</option>" for k, v in CREDIT_REASONS.items())
        credit_form = f"""
              <form class="inline" method="POST" action="/add_credit?ym={ym}">
                <label>Emp ID <input name="emp_id" required size="6" placeholder="E001"></label>
                <label>Date Worked <input type="date" name="work_date" required max="{date.today().isoformat()}"></label>
                <label>Reason <select name="reason">{reason_opts}</select></label>
                <label>Note <input name="note" size="30" placeholder="Work done; after {credit_days} days, why the delay is condoned"></label>
                <button class="btn" type="submit">{'Grant Credit' if role == ROLE_APPROVER else 'Recommend Credit'}</button>
              </form>"""
    body += f"""
            <div class="panel">
              <h3>Leave Credits (Holiday / Weekend Worked)</h3>
              <p class="note">One credit leave for each <b>complete working day</b> spent at work on a gazetted holiday, or on a Saturday / Sunday that is the person's day off.
                <b>Staff apply within {credit_days} days</b> of the date worked; a later application moves forward only if the <b>reviewer condones the delay</b>.
                The reviewer recommends it and the credit counts only after the <b>approver</b> approves it. The same rule applies to <b>all staff, including ICAP / NOC students</b>.
                An approved credit adds to the leave entitlement and covers leave that would otherwise be deducted.</p>
              {apply_credit_form}
              {credit_form}
              <table><thead><tr><th>ID</th><th>Name</th><th>Date Worked</th><th class="num">Days</th><th>Reason</th><th>Source</th><th>By</th><th>Status</th><th></th></tr></thead>
                <tbody>{credit_rows or "<tr><td colspan='9'>No leave credits yet.</td></tr>"}</tbody></table>
            </div>"""
    return body
