"""The approvals tab of the web app."""

import html as html_lib

from common.constants import CREDIT_REASONS
from common.dates import parse_date
from leaves.rules import credit_review_form
from payroll.changes import KIND_LABELS

def render(self, approver_only, conn, is_approver, ym, body=""):
    pending = conn.execute("SELECT * FROM change_requests WHERE status = 'Pending' ORDER BY id").fetchall()
    history = conn.execute("SELECT * FROM change_requests WHERE status != 'Pending' ORDER BY id DESC LIMIT 100").fetchall()

    def decision_buttons(r):
        if not is_approver:
            return "<span class='badge b-warn'>Awaiting approver</span>"
        return (f"<div style='display:flex; gap:6px;'>"
                f"<form method='POST' action='/approve_request?ym={ym}'><input type='hidden' name='id' value='{r['id']}'><button class='btn btn-ok' type='submit'>Approve</button></form>"
                f"<form method='POST' action='/reject_request?ym={ym}'><input type='hidden' name='id' value='{r['id']}'><button class='btn btn-danger' type='submit'>Reject</button></form></div>")

    type_names = {t["code"]: t["name"] for t in conn.execute("SELECT code, name FROM doc_types")}

    def request_docs(r):
        if r["kind"] != "add_staff":
            return ""
        docs = conn.execute("SELECT id, doc_type FROM staff_documents WHERE change_request_id = ?", (r["id"],)).fetchall()
        return ("<br><span class='note'>Documents: " + (", ".join(
            f"<a href='/staff_doc?id={d['id']}' target='_blank'>{html_lib.escape(type_names.get(d['doc_type'], d['doc_type']))}</a>" for d in docs)
            or "none") + "</span>")

    pending_rows = "".join(f"""
                <tr>
                  <td>#{r['id']}</td><td>{r['requested_at'].replace('T', ' ')}</td><td>{html_lib.escape(r['requested_by'])}</td>
                  <td>{KIND_LABELS.get(r['kind'], r['kind'])}</td><td style="white-space:normal;">{html_lib.escape(r['summary'])}{request_docs(r)}</td>
                  <td>{decision_buttons(r)}</td>
                </tr>""" for r in pending) or "<tr><td colspan='6'><em>No changes waiting for approval.</em></td></tr>"
    history_rows = "".join(f"""
                <tr>
                  <td>#{r['id']}</td><td>{r['requested_at'].replace('T', ' ')}</td><td>{html_lib.escape(r['requested_by'])}</td>
                  <td>{KIND_LABELS.get(r['kind'], r['kind'])}</td><td style="white-space:normal;">{html_lib.escape(r['summary'])}</td>
                  <td><span class="badge {'b-ok' if r['status'] == 'Approved' else 'b-warn'}">{r['status']}</span></td>
                  <td>{html_lib.escape(r['decided_by'] or '')}</td><td>{(r['decided_at'] or '').replace('T', ' ')}</td>
                </tr>""" for r in history) or "<tr><td colspan='8'><em>No decisions yet.</em></td></tr>"
    waiting_credits = [c for c in conn.execute("""SELECT c.*, s.employee_name FROM leave_credits c JOIN staff s ON s.emp_id = c.emp_id
                                                          WHERE c.status IN ('Recommended', 'Applied', 'Late') AND c.source != 'Timesheet' ORDER BY c.id""").fetchall()
                       if c["status"] == "Recommended" or c["emp_id"] in approver_only]
    credit_btns = lambda c: ("<span class='badge b-warn'>Awaiting approver</span>" if not is_approver else
                             credit_review_form(c, ym, "Condone &amp; Approve" if c["status"] == "Late" else "Approve") if c["status"] != "Recommended" else
                             "<div style='display:flex; gap:6px;'>" + "".join(
                                 f"<form method='POST' action='/decide_credit?ym={ym}'><input type='hidden' name='id' value='{c['id']}'>"
                                 f"<input type='hidden' name='decision' value='{d}'><button class='btn {css}' type='submit'>{label}</button></form>"
                                 for d, label, css in (("Approved", "Approve", "btn-ok"), ("Rejected", "Reject", "btn-danger"))) + "</div>")
    credits_panel = f"""
            <div class="panel">
              <h3>Leave Credits Waiting for Approval</h3>
              <p class="note">Recommended by the reviewer for working on a gazetted holiday or being called in on a weekend. A credit adds
                to the person's leave only after approval.</p>
              <table><thead><tr><th>Recommended</th><th>By</th><th>Staff</th><th>Date Worked</th><th class="num">Days</th><th>Reason</th><th>Decision</th></tr></thead>
                <tbody>{"".join(f"<tr><td>{c['requested_at'][:16].replace('T', ' ')}</td><td>{html_lib.escape(c['requested_by'] or '')}</td><td>{c['emp_id']} {html_lib.escape(c['employee_name'])}</td><td>{parse_date(c['work_date']).strftime('%a %d %b %Y')}</td><td class='num'>{c['days']:g}</td><td>{CREDIT_REASONS.get(c['reason'], c['reason'])}{('<br><span class=note>' + html_lib.escape(c['note']) + '</span>') if c['note'] else ''}</td><td>{credit_btns(c)}</td></tr>" for c in waiting_credits)}</tbody></table>
            </div>""" if waiting_credits else ""
    waiting_loans = [a for a in conn.execute("""SELECT a.*, s.employee_name FROM loan_applications a JOIN staff s ON s.emp_id = a.emp_id
                                                        WHERE a.status IN ('Applied', 'Recommended') ORDER BY a.id""").fetchall()
                     if a["status"] == "Recommended" or a["emp_id"] in approver_only]
    loans_panel = f"""
            <div class="panel">
              <h3>Loan / Advance Applications Waiting for Approval</h3>
              <table><thead><tr><th>#</th><th>Staff</th><th>Type</th><th class="num">Amount</th><th>Policy</th><th>Recommended By</th><th></th></tr></thead>
                <tbody>{"".join(f"<tr><td>#{a['id']}</td><td>{a['emp_id']} {html_lib.escape(a['employee_name'])}</td><td>{a['loan_type']}</td><td class='num'>{a['amount']:,.0f}</td><td>{'<span class=\"badge b-warn\">Exceeds policy — exception</span>' if a['exceeds_policy'] else '<span class=\"badge b-ok\">Within policy</span>'}</td><td>{html_lib.escape(a['reviewed_by'] or '(goes straight to approver)')}</td><td><a class='btn btn-sec' href='/?tab=loans&ym={ym}'>Decide on Loans tab</a></td></tr>" for a in waiting_loans)}</tbody></table>
            </div>""" if waiting_loans else ""
    body = credits_panel + loans_panel + f"""
            <div class="panel">
              <h3>Waiting for Approval</h3>
              <p class="note">New staff members, staff / pay changes, monthly adjustments and loans entered by a Preparer
                change nothing until the approver approves them here. Each month's payroll must also be approved on the
                Monthly Payroll tab before bank files can be exported; an approved month is locked.</p>
              <table>
                <thead><tr><th>Ref</th><th>Requested</th><th>By</th><th>Type</th><th>Change</th><th>Decision</th></tr></thead>
                <tbody>{pending_rows}</tbody>
              </table>
            </div>
            <div class="panel">
              <h3>Approval Log</h3>
              <table>
                <thead><tr><th>Ref</th><th>Requested</th><th>By</th><th>Type</th><th>Change</th><th>Status</th><th>Decided By</th><th>Decided</th></tr></thead>
                <tbody>{history_rows}</tbody>
              </table>
            </div>"""
    return body
