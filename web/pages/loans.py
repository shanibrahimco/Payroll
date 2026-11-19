"""The loans tab of the web app."""

from datetime import date
import html as html_lib
import json

from auth.roles import ROLE_ACTIONS, ROLE_APPROVER, ROLE_PREPARER, ROLE_REVIEWER
from common.constants import EOBI_ONLY_TYPE
from common.dates import add_months, first_of_month
from common.settings import get_settings_dict
from loans.rules import LUMP_SUM, loan_balance, loan_outstanding, loan_repaid
from payroll.bank_files import loans_to_disburse
from payroll.calculator import effective_salary

def render(self, approver_only, cannot_decide, conn, is_approver, is_staff, own_emp, pm_str, role, ym, body=""):
    esc = html_lib.escape
    cfg = get_settings_dict(conn)
    only_emp = (own_emp or "") if is_staff else None  # Staff see only their own records
    max_sal, max_inst = float(cfg.get("loan_max_salaries", 2)), int(float(cfg.get("loan_max_instalments", 20)))
    lns = conn.execute("""SELECT a.*, s.employee_name FROM advances_loans a JOIN staff s ON a.emp_id=s.emp_id
                                  WHERE ? IS NULL OR a.emp_id = ? ORDER BY a.loan_ref""", (only_emp, only_emp)).fetchall()
    this_month, today = first_of_month(date.today()), date.today().isoformat()
    repaid = {l["loan_ref"]: loan_repaid(conn, l["loan_ref"]) for l in lns}
    balance = {l["loan_ref"]: loan_balance(l, this_month, repaid[l["loan_ref"]]) for l in lns}

    def repayment_cell(l):
        if l["repayment_method"] == LUMP_SUM:
            overdue = l["lump_sum_due"] and l["lump_sum_due"] < today and balance[l["loan_ref"]] > 0
            return (f"Lump sum by {esc(l['lump_sum_due'] or '')}"
                    + (" <span class='badge b-warn'>Overdue</span>" if overdue else ""))
        return f"{l['monthly_instalment']:,.0f}"

    def balance_cell(l):
        left = balance[l["loan_ref"]]
        return "<span class='badge b-ok'>Repaid</span>" if left <= 0 else f"{left:,.0f}"

    rows_html = "".join(f"""
                <tr>
                  <td>{l['loan_ref']}</td><td>{l['emp_id']}</td><td>{esc(l['employee_name'])}</td>
                  <td>{l['loan_type']}</td><td>{l['date_given']}</td><td class="num">{l['amount']:,.0f}</td>
                  <td class="num">{repayment_cell(l)}</td><td>{l['first_recovery_month']}</td>
                  <td>{'Yes' if l['hold_this_month'] else 'No'}</td><td class="num">{repaid[l['loan_ref']]:,.0f}</td><td class="num">{balance_cell(l)}</td>
                  <td>{esc(l['purpose'] or '')}</td><td>{esc(l['remarks'] or '')}</td>
                  <td>{"<span class='badge b-warn'>Awaiting payment</span>" if not l['disbursed'] else
                       f"<span class='badge b-ok'>Paid {(l['disbursed_at'] or '')[:10]}</span><br><span class='note'>{l['disbursement_batch']}</span>" if l['disbursement_batch'] else "Paid"}</td>
                </tr>""" for l in lns)
    loans_head = ("<thead><tr><th>Ref</th><th>ID</th><th>Name</th><th>Type</th><th>Date</th><th class='num'>Amount</th><th class='num'>Instalment / Lump Sum</th>"
                  "<th>1st Rec.</th><th>Hold?</th><th class='num'>Repaid Directly</th><th class='num'>Balance</th><th>Purpose</th><th>Remarks</th><th>Paid</th></tr></thead>")

    repayments = conn.execute("""SELECT r.*, s.employee_name FROM loan_repayments r JOIN staff s ON s.emp_id = r.emp_id
                                 WHERE ? IS NULL OR r.emp_id = ? ORDER BY r.paid_on DESC, r.id DESC""", (only_emp, only_emp)).fetchall()
    repay_rows = "".join(f"<tr><td>{esc(r['paid_on'])}</td><td>{esc(r['loan_ref'])}</td><td>{r['emp_id']} {esc(r['employee_name'])}</td>"
                         f"<td class='num'>{r['amount']:,.0f}</td><td>{esc(r['note'] or '')}</td><td>{esc(r['recorded_by'] or '')}</td></tr>" for r in repayments)
    repay_form = ""
    if role in ROLE_ACTIONS["/record_loan_repayment"]:
        open_loans = [l for l in lns if balance[l["loan_ref"]] > 0]
        options = "".join(f"<option value='{esc(l['loan_ref'], quote=True)}'>{esc(l['loan_ref'])} — {l['emp_id']} {esc(l['employee_name'])} "
                          f"(balance Rs. {balance[l['loan_ref']]:,.0f})</option>" for l in open_loans)
        repay_form = f"""
              <form class="inline" method="POST" action="/record_loan_repayment?ym={ym}">
                <label>Loan <select name="loan_ref" required>{options}</select></label>
                <label>Amount Repaid (Rs.) <input type="number" name="amount" min="1" required style="width:110px;"></label>
                <label>Date Paid <input type="date" name="paid_on" max="{today}" required></label>
                <label>Note <input name="note" size="24" placeholder="e.g. Cash, receipt no."></label>
                <button class="btn" type="submit"{'' if open_loans else ' disabled'}>Record Repayment</button>
              </form>"""
    repay_panel = f"""
            <div class="panel">
              <h3>{'My Loan Repayments' if is_staff else 'Loan Repayments Made Directly'}</h3>
              <p class="note">Money paid back by cash or bank transfer, outside the payroll. It reduces the balance, and salary deductions
                stop once the loan is repaid. Only the approver records repayments.</p>
              {repay_form}
              <table><thead><tr><th>Date Paid</th><th>Loan</th><th>Staff</th><th class="num">Amount</th><th>Note</th><th>Recorded By</th></tr></thead>
                <tbody>{repay_rows or "<tr><td colspan='6'>No direct repayments yet.</td></tr>"}</tbody></table>
            </div>"""
    pay_panel = ""
    if role in ROLE_ACTIONS["/export_loans"]:
        waiting = loans_to_disburse(conn)
        last = json.loads(cfg["last_loan_batch"]) if cfg.get("last_loan_batch") else None
        last_html = ""
        if last:
            files = " and ".join(f"<code>{esc(p)}</code>" for p in (last["ibft"], last["ift"]) if p)
            last_html = (f"<p class='note'>Last batch <b>{last['batch']}</b> by {esc(last['by'])} on {last['at'][:16].replace('T', ' ')}: "
                         f"{len(last['paid'])} loan(s) — files {files}."
                         + (f"<br><b>Left out (no bank account / IBFT code):</b> {esc(', '.join(last['skipped']))}" if last["skipped"] else "") + "</p>")
        wait_rows = "".join(
            f"<tr><td>{w['loan_ref']}</td><td>{w['emp_id']} {esc(w['employee_name'])}</td><td>{w['loan_type']}</td><td class='num'>{w['amount']:,.0f}</td>"
            f"<td>{w['approval_date'] or ''}</td><td>{esc(w['bank_name'] or '')}</td>"
            f"<td>{'Meezan IFT' if w['is_meezan'] == 1 or w['bank_name'] == 'Meezan Bank' else 'IBFT' if w['ibft_bank_code'] else '<span class=\"badge b-warn\">No IBFT bank code</span>'}"
            f"{'' if w['bank_account_iban'] else ' <span class=\"badge b-warn\">No bank account</span>'}</td></tr>" for w in waiting)
        pay_panel = f"""
            <div class="panel">
              <h3>Approved Loans Awaiting Payment</h3>
              <p class="note">Loans and advances are <b>paid by separate IBFT / Meezan IFT files</b>, never with salaries. Generating the files
                marks the loans in them as paid (with the batch number), so a loan can't be paid twice. Recovery from salary starts the month
                after approval.</p>
              <table><thead><tr><th>Ref</th><th>Staff</th><th>Type</th><th class="num">Amount</th><th>Approved</th><th>Bank</th><th>Paid By</th></tr></thead>
                <tbody>{wait_rows or "<tr><td colspan='7'>Nothing waiting to be paid.</td></tr>"}</tbody></table>
              <form method="POST" action="/export_loans?ym={ym}" style="margin-top:12px;">
                <button class="btn btn-ok" type="submit"{'' if waiting else ' disabled'}>Generate Loan Bank Files (IBFT / IFT) — Rs. {sum(w['amount'] for w in waiting):,.0f}</button>
              </form>
              {last_html}
            </div>"""
    apps = conn.execute("""SELECT a.*, s.employee_name FROM loan_applications a JOIN staff s ON s.emp_id = a.emp_id
                                   WHERE ? IS NULL OR a.emp_id = ? ORDER BY a.id DESC LIMIT 200""", (only_emp, only_emp)).fetchall()
    reviewer_emps = approver_only

    def app_status(a):
        st = a["status"]
        out = f"<span class='badge {'b-ok' if st == 'Approved' else 'b-warn'}'>{st}</span>"
        if a["reviewed_by"]:
            out += f"<br><span class='note'>{'Recommended' if st != 'Rejected' or a['decided_by'] else 'Rejected'} by {esc(a['reviewed_by'])}</span>"
        if a["review_note"]:
            out += f"<br><span class='note'>“{esc(a['review_note'])}”</span>"
        if a["decided_by"]:
            approved = f" — Rs. {a['approved_amount']:,.0f} as {a['loan_ref']}" if st == "Approved" else ""
            out += f"<br><span class='note'>{st} by {esc(a['decided_by'])}{approved}</span>"
        return out

    def app_action(a):
        st = a["status"]
        if st == "Applied" and a["emp_id"] == own_emp:
            return (f"<form method='POST' action='/withdraw_loan?ym={ym}'><input type='hidden' name='id' value='{a['id']}'>"
                    f"<button class='btn btn-sec' type='submit'>Withdraw</button></form>")
        if cannot_decide(a["emp_id"]):
            return ""
        if role == ROLE_REVIEWER and st == "Applied":
            exc = ("<label style='flex-direction:row; gap:4px; align-items:center;'><input type='checkbox' name='exception' value='1'>"
                   "Recommend as exception</label>") if a["exceeds_policy"] else ""
            return (f"<form method='POST' action='/review_loan?ym={ym}' style='display:flex; gap:6px; flex-wrap:wrap; align-items:center;'>"
                    f"<input type='hidden' name='id' value='{a['id']}'>{exc}"
                    f"<input name='note' size='22' placeholder='{'Justification (required)' if a['exceeds_policy'] else 'Note / reason'}'>"
                    f"<button class='btn btn-ok' type='submit' name='decision' value='Recommended'>Recommend</button>"
                    f"<button class='btn btn-danger' type='submit' name='decision' value='Rejected'>Reject</button></form>")
        if role == ROLE_APPROVER and (st == "Recommended" or (st == "Applied" and a["emp_id"] in reviewer_emps)):
            return (f"<form method='POST' action='/decide_loan?ym={ym}' style='display:flex; gap:6px; flex-wrap:wrap; align-items:center;'>"
                    f"<input type='hidden' name='id' value='{a['id']}'>"
                    f"<label>Approve Rs. <input type='number' name='approved_amount' value='{a['amount']:.0f}' max='{a['amount']:.0f}' min='1' style='width:95px;'></label>"
                    f"<input name='note' size='16' placeholder='Note'>"
                    f"<button class='btn btn-ok' type='submit' name='decision' value='Approved'>Approve</button>"
                    f"<button class='btn btn-danger' type='submit' name='decision' value='Rejected'>Reject</button></form>")
        return ""

    def policy_cell(a):
        avail = a["policy_limit"] - a["outstanding"]
        if a["exceeds_policy"]:
            return (f"<span class='badge b-warn'>Exceeds policy</span><br><span class='note'>limit Rs. {a['policy_limit']:,.0f}, "
                    f"already Rs. {a['outstanding']:,.0f}; additional Rs. {a['amount'] - max(0, avail):,.0f}</span>")
        return f"<span class='badge b-ok'>Within policy</span><br><span class='note'>limit Rs. {a['policy_limit']:,.0f}</span>"

    app_rows = "".join(f"""
                <tr><td>#{a['id']}</td><td>{a['applied_at'][:10]}</td><td>{a['emp_id']} {esc(a['employee_name'])}</td><td>{a['loan_type']}</td>
                  <td class="num"><strong>{a['amount']:,.0f}</strong></td>
                  <td class="num">{('Lump sum by ' + esc(a['lump_sum_date'] or '')) if a['loan_type'] == 'Loan' and a['repayment_method'] == LUMP_SUM else a['instalments'] if a['loan_type'] == 'Loan' else '1 (next month)'}</td>
                  <td>{esc(a['purpose'] or '')}</td><td>{policy_cell(a)}</td><td>{app_status(a)}</td><td>{app_action(a)}</td></tr>""" for a in apps)

    apply_panel = ""
    if own_emp:
        me = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (own_emp,)).fetchone()
        if me is not None and me["staff_type"] != EOBI_ONLY_TYPE:
            my_limit = effective_salary(conn, me, pm_str, cfg) * max_sal
            my_out = loan_outstanding(conn, own_emp, pm_str)
            apply_panel = f"""
            <div class="panel">
              <h3>Apply for a Loan or Advance</h3>
              <p class="note">Policy: loans and advances outstanding may not exceed <b>{max_sal:g} months' salary</b> (your limit:
                <b>Rs. {my_limit:,.0f}</b>; already outstanding or applied for: <b>Rs. {my_out:,.0f}</b>; available:
                <b>Rs. {max(0, my_limit - my_out):,.0f}</b>). A loan is repaid either in at most <b>{max_inst} monthly instalments</b> from
                salary, or in <b>one lump sum</b> by a date you commit to (within {max_inst} months; nothing is deducted from salary meanwhile).
                An advance is recovered in full from next month's salary. Every application is reviewed by the reviewer and approved by the approver.
                An amount above the limit is possible only if the reviewer recommends it as an exception and the approver approves it.</p>
              <form class="inline" method="POST" action="/apply_loan?ym={ym}">
                <label>Type <select name="loan_type"><option>Loan</option><option>Advance</option></select></label>
                <label>Amount (Rs.) <input type="number" name="amount" min="1" required style="width:110px;"></label>
                <label>Repayment (loan) <select name="repayment"><option value="Instalments">Monthly instalments</option><option value="{LUMP_SUM}">Lump sum by a date</option></select></label>
                <label>Instalments (if monthly) <input type="number" name="instalments" min="1" max="{max_inst}" value="{max_inst}" style="width:80px;"></label>
                <label>Lump sum date (if lump sum) <input type="date" name="lump_sum_date" min="{date.today().isoformat()}" max="{add_months(date.today(), max_inst).isoformat()}"></label>
                <label>Purpose <input name="purpose" required size="30" placeholder="Reason for the loan"></label>
                <button class="btn" type="submit">Apply</button>
              </form>
            </div>"""
    n_waiting = sum(1 for a in apps if app_action(a) and a["emp_id"] != own_emp)
    apps_panel = f"""
            <div class="panel">
              <h3>{'My Loan Applications' if is_staff else 'Loan &amp; Advance Applications'}{f" — <span class='badge b-warn'>{n_waiting} waiting for you</span>" if n_waiting else ""}</h3>
              <table><thead><tr><th>#</th><th>Applied</th><th>Staff</th><th>Type</th><th class="num">Amount</th><th class="num">Instalments</th><th>Purpose</th><th>Policy</th><th>Status</th><th></th></tr></thead>
                <tbody>{app_rows or "<tr><td colspan='10'>No applications yet.</td></tr>"}</tbody></table>
            </div>"""
    loans_table = f"""
            <div class="panel">
              <h3>{'My Loans &amp; Advances' if is_staff else 'Loans &amp; Advances'}</h3>
              <table>
                {loans_head}
                <tbody>{rows_html or "<tr><td colspan='14'>None.</td></tr>"}</tbody>
              </table>
            </div>"""
    if is_staff:
        body = apply_panel + apps_panel + loans_table + repay_panel
    else:
        body = apply_panel + apps_panel + pay_panel + repay_panel + f"""
            <div class="panel">
              <h3>Add / Update Advance or Loan Directly (e.g. earlier loans)</h3>
              <p class="note">{'Saved immediately and recorded in the approval log.' if is_approver else '<b>Sent to the approver; recovery starts only after approval.</b>' if role == ROLE_PREPARER else '<b>View only.</b>'}
                At most {max_inst} instalments; an amount above {max_sal:g} salaries is flagged for the approver.</p>
              <form class="inline" method="POST" action="/add_loan?ym={ym}">
                <label>Ref <input name="loan_ref" placeholder="L007" required size="6"></label>
                <label>Emp ID <input name="emp_id" placeholder="E001" required size="6"></label>
                <label>Type <select name="loan_type"><option>Loan</option><option>Advance</option></select></label>
                <label>Date Given <input type="date" name="date_given" required></label>
                <label>Amount <input type="number" name="amount" required style="width:100px;"></label>
                <label>Monthly Inst. <input type="number" name="monthly_instalment" value="0" style="width:100px;"></label>
                <label>Purpose <input name="purpose" placeholder="Purpose"></label>
                <button class="btn" type="submit">Save Loan</button>
              </form>
              <table>
                {loans_head}
                <tbody>{rows_html}</tbody>
              </table>
            </div>"""
    return body
