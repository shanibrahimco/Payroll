"""The staff tab of the web app."""

import html as html_lib

import config
from auth.roles import ROLE_ACTIONS, ROLE_APPROVER, ROLE_PREPARER
from common.constants import ARTICLE_TYPES, EOBI_ONLY_TYPE, NOC_TYPE, STAFF_TYPE_OPTIONS
from common.dates import parse_date
from common.settings import get_settings_dict
from payroll.calculator import weekly_hours_for
from payroll.changes import training_org_of
from staff.documents import doc_types_for, missing_documents
from staff.rules import cnic_alert, eobi_start_date

def render(self, conn, is_approver, pm_str, qs, role, ym, body=""):
    staff = conn.execute("SELECT * FROM staff ORDER BY emp_id").fetchall()
    cfg = get_settings_dict(conn)
    eobi_wait = int(float(cfg.get("eobi_permanent_wait_months", 3)))
    std_week_hours = float(cfg.get("standard_weekly_hours", 45))

    month_hours = {s["emp_id"]: weekly_hours_for(conn, s, pm_str) for s in staff}

    def salary_cell(s):
        hours = month_hours[s["emp_id"]]
        if not hours:
            return f"{s['monthly_salary']:,.0f}"
        paid = round(s["monthly_salary"] * hours / std_week_hours, 0)
        return f"{paid:,.0f}<br><span style='color:#64748b; font-size:11px;'>{s['monthly_salary']:,.0f} × {hours:g}/{std_week_hours:g}h</span>"

    def eobi_status(s):
        if s["status"] == "Left":
            left = parse_date(s["leaving_date"])
            return f"Not applicable (left {left.strftime('%d %b %Y')})" if left else "Not applicable (left)"
        stype = s["staff_type"] or ""
        eobi_reg = parse_date(s["eobi_registration_date"])
        start = eobi_start_date(stype, parse_date(s["date_of_joining"]), parse_date(s["icap_reg_confirmed_date"]), eobi_reg, eobi_wait)
        if start and eobi_reg:
            return f"<span class='badge b-ok'>Registered from {start.strftime('%d %b %Y')}</span>"
        if start:
            return f"<span class='badge b-warn'>Due from {start.strftime('%d %b %Y')} (register)</span>"
        if stype == "Permanent":
            return "<span class='badge b-warn'>Enter joining date</span>"
        if stype in ARTICLE_TYPES:
            return "<span class='badge b-warn'>Awaiting ICAP registration</span>"
        if stype == "Intern":
            return "Not applicable"
        if stype == EOBI_ONLY_TYPE:
            return "<span class='badge b-warn'>Enter EOBI registration date</span>"
        return "<span class='badge b-warn'>Set staff type</span>"

    esc = html_lib.escape
    active_staff = [s for s in staff if s["status"] != "Left" and s["staff_type"] != EOBI_ONLY_TYPE]
    missing_docs = {s["emp_id"]: missing_documents(conn, s["emp_id"], s["staff_type"]) for s in active_staff}

    def cnic_cell(s):
        warn = cnic_alert(s["cnic_expiry"])
        if not s["cnic_expiry"]:
            return "" if s["staff_type"] == EOBI_ONLY_TYPE or s["status"] == "Left" else "<span class='note'>—</span>"
        return f"<span class='badge b-warn'>{warn}</span>" if warn else s["cnic_expiry"]

    def docs_cell(s):
        if s["emp_id"] not in missing_docs:
            return ""
        miss = missing_docs[s["emp_id"]]
        label = (f"<span class='badge b-warn'>{len(miss)} missing</span>" if miss else "<span class='badge b-ok'>Complete</span>")
        return f"<a href='/?tab=staff&ym={ym}&emp={s['emp_id']}#docs' title='{esc(', '.join(miss), quote=True)}'>{label}</a>"

    rows_html = "".join(f"""
                <tr>
                  <td>{s['emp_id']}</td><td><strong>{s['employee_name']}</strong></td><td>{s['cnic'] or ''}</td><td>{cnic_cell(s)}</td>
                  <td>{s['staff_type'] or ''}{f"<br><span class='note'>{s['leave_entitlement_override'] or 0:g} residual leaves, to {s['articleship_end_date'] or '?'}</span>" if s['staff_type'] == NOC_TYPE else ''}</td>
                  <td>{'n/a' if s['staff_type'] == EOBI_ONLY_TYPE else s['date_of_joining'] or "<span class='badge b-warn'>Not entered</span>"}</td>
                  <td>{s['eobi_registration_date'] or ''}</td><td>{s['eobi_no'] or ''}</td><td>{eobi_status(s)}</td>
                  <td>{s['icap_reg_confirmed_date'] or ''}</td><td>{s['icap_crn'] or ''}</td>
                  <td>{esc(training_org_of(s)) if s['staff_type'] in ARTICLE_TYPES else ''}</td>
                  <td class="num">{month_hours[s['emp_id']] or std_week_hours:g}</td><td class="num">{salary_cell(s)}</td><td>{s['bank_name'] or ''}</td>
                  <td>{s['bank_account_iban'] or ''}</td><td class="num">{s['opening_leaves_cumulative']}</td>
                  <td class="num">{s['opening_withheld_probation']:,.0f}</td><td>{s['status']}</td><td>{docs_cell(s)}</td>
                </tr>""" for s in staff)

    # CNIC alerts, documents of one staff member, and (approver) which documents are required
    alerts = [(s, cnic_alert(s["cnic_expiry"])) for s in active_staff if cnic_alert(s["cnic_expiry"])]
    no_cnic_date = [s for s in active_staff if not s["cnic_expiry"]]
    alerts_panel = ""
    if alerts or no_cnic_date:
        alerts_panel = f"""
            <div class="panel" style="border-left:4px solid #dc2626;">
              <h3>CNIC Alerts</h3>
              {"".join(f"<p class='note' style='margin:2px 0;'><span class='badge b-warn'>{esc(w)}</span> {s['emp_id']} <b>{esc(s['employee_name'])}</b> ({s['cnic'] or 'no CNIC no.'}) — obtain the renewed CNIC and upload it below.</p>" for s, w in alerts)}
              {f"<p class='note' style='margin:6px 0 0 0;'>CNIC expiry date not entered: {esc(', '.join(s['emp_id'] + ' ' + s['employee_name'] for s in no_cnic_date))}.</p>" if no_cnic_date else ""}
            </div>"""
    doc_types_all = conn.execute("SELECT * FROM doc_types ORDER BY active DESC, mandatory DESC, name").fetchall()
    type_names = {t["code"]: t["name"] for t in doc_types_all}
    sel = "".join(c for c in qs.get("emp", [""])[0].upper() if c.isalnum())
    sel_staff = next((s for s in staff if s["emp_id"] == sel), None)
    docs_panel_inner = "<p class='note'>Choose a staff member to see and add their documents.</p>"
    if sel_staff is not None:
        docs = conn.execute("SELECT * FROM staff_documents WHERE emp_id = ? ORDER BY replaced, doc_type, id DESC", (sel,)).fetchall()
        doc_rows = "".join(
            f"<tr{' style=\"color:#94a3b8;\"' if d['replaced'] else ''}><td>{esc(type_names.get(d['doc_type'], d['doc_type']))}</td>"
            f"<td><a href='/staff_doc?id={d['id']}' target='_blank'>{esc(d['original_name'] or 'view')}</a></td>"
            f"<td>{d['expiry_date'] or ''}{(' <span class=\"badge b-warn\">' + esc(cnic_alert(d['expiry_date'])) + '</span>') if d['doc_type'] == 'cnic' and not d['replaced'] and cnic_alert(d['expiry_date']) else ''}</td>"
            f"<td>{esc(d['uploaded_by'] or '')} {(d['uploaded_at'] or '')[:10]}</td><td>{'Older copy' if d['replaced'] else 'Current'}</td></tr>" for d in docs)
        miss = missing_documents(conn, sel, sel_staff["staff_type"])
        upload_form = ""
        if role in ROLE_ACTIONS["/upload_document"]:
            opts = "".join(f"<option value='{t['code']}'>{esc(t['name'])}</option>" for t in doc_types_for(conn, sel_staff["staff_type"]))
            upload_form = f"""
                  <form class="inline" method="POST" action="/upload_document?ym={ym}&emp={sel}" enctype="multipart/form-data">
                    <label>Document <select name="doc_type">{opts}</select></label>
                    <label>File (PDF / JPG / PNG) <input type="file" name="document" accept=".pdf,.jpg,.jpeg,.png" required></label>
                    <label>Expiry Date (CNIC) <input type="date" name="expiry_date"></label>
                    <button class="btn" type="submit">Upload</button>
                  </form>"""
        docs_panel_inner = f"""
                  <p class="note"><b>{sel} {esc(sel_staff['employee_name'])}</b> — CNIC {sel_staff['cnic'] or 'not entered'},
                    expiry {sel_staff['cnic_expiry'] or 'not entered'}.
                    {f"<span class='badge b-warn'>Missing: {esc(', '.join(miss))}</span>" if miss else "<span class='badge b-ok'>All mandatory documents on file</span>"}</p>
                  {upload_form}
                  <table><thead><tr><th>Document</th><th>File</th><th>Expiry</th><th>Uploaded</th><th></th></tr></thead>
                    <tbody>{doc_rows or "<tr><td colspan='5'>No documents yet.</td></tr>"}</tbody></table>"""
    staff_opts = "".join(f"<option value='{s['emp_id']}'{' selected' if s['emp_id'] == sel else ''}>{s['emp_id']} — {esc(s['employee_name'])}</option>"
                         for s in staff if s["staff_type"] != EOBI_ONLY_TYPE)
    docs_panel = f"""
            <div class="panel" id="docs">
              <h3>Staff Documents</h3>
              <form class="inline" method="GET" action="/">
                <input type="hidden" name="tab" value="staff"><input type="hidden" name="ym" value="{ym}">
                <label>Staff Member <select name="emp" onchange="this.form.submit()"><option value="">(choose)</option>{staff_opts}</select></label>
                <noscript><button class="btn btn-sec" type="submit">Show</button></noscript>
              </form>
              {docs_panel_inner}
            </div>"""
    type_rows = "".join(
        f"<tr{'' if t['active'] else ' style=\"color:#94a3b8;\"'}><td><b>{esc(t['name'])}</b></td><td>{t['applies_to']}</td>"
        f"<td>{'Mandatory' if t['mandatory'] else 'Optional'}</td><td>{'Yes' if t['has_expiry'] else ''}</td><td>{'Active' if t['active'] else 'Not used'}</td>"
        + (f"<td style='display:flex; gap:6px;'>" + "".join(
            f"<form method='POST' action='/toggle_doc_type?ym={ym}'><input type='hidden' name='code' value='{t['code']}'>"
            f"<input type='hidden' name='field' value='{f}'><button class='btn btn-sec' type='submit'>{label}</button></form>"
            for f, label in (("mandatory", "Make Optional" if t["mandatory"] else "Make Mandatory"), ("active", "Stop Using" if t["active"] else "Use Again")))
           + "</td>" if role in ROLE_ACTIONS["/toggle_doc_type"] else "") + "</tr>" for t in doc_types_all)
    add_type_form = f"""
              <form class="inline" method="POST" action="/save_doc_type?ym={ym}">
                <label>Document <input name="name" required size="26" placeholder="e.g. Character certificate"></label>
                <label>Required For <select name="applies_to"><option>All</option><option>Permanent</option><option value="ICAP Article">ICAP Article (and NOC)</option><option value="NOC Article">NOC Article only</option><option>Intern</option></select></label>
                <label style="flex-direction:row; align-items:center; gap:4px;"><input type="checkbox" name="mandatory" value="1" checked> Mandatory</label>
                <label style="flex-direction:row; align-items:center; gap:4px;"><input type="checkbox" name="has_expiry" value="1"> Has expiry date</label>
                <button class="btn" type="submit">Add Requirement</button>
              </form>""" if role in ROLE_ACTIONS["/save_doc_type"] else ""
    types_panel = f"""
            <div class="panel">
              <h3>Required Documents for New Staff</h3>
              <p class="note">A new staff member can only be submitted with every <b>mandatory</b> document for their staff type attached.
                {'You decide which documents are required.' if is_approver else 'Only the approver can change these requirements.'}</p>
              {add_type_form}
              <table><thead><tr><th>Document</th><th>Required For</th><th></th><th>Expiry</th><th>Status</th>{'<th></th>' if role in ROLE_ACTIONS['/toggle_doc_type'] else ''}</tr></thead>
                <tbody>{type_rows}</tbody></table>
            </div>"""
    new_doc_inputs = "".join(
        f"<label>{esc(t['name'])}{' *' if t['mandatory'] else ''}{'' if t['applies_to'] == 'All' else ' (' + t['applies_to'] + ')'}"
        f" <input type='file' name='doc_{t['code']}' accept='.pdf,.jpg,.jpeg,.png'></label>"
        + (f"<label>{esc(t['name'])} expiry <input type='date' name='expiry_{t['code']}'></label>" if t["has_expiry"] and t["code"] != "cnic" else "")
        for t in doc_types_all if t["active"])
    type_options = "".join(f'<option value="{v}">{label}</option>' for v, label in STAFF_TYPE_OPTIONS)
    training_options = "".join(f'<option value="{esc(o, quote=True)}">{esc(o)}</option>' for o in config.TRAINING_ORGS)
    approval_note = {ROLE_APPROVER: "Your changes apply immediately and are recorded in the approval log.",
                     ROLE_PREPARER: "<b>Changes you save are sent to the approver and take effect only after approval.</b>",
                     }.get(role, "<b>View only</b> — your role can't change staff or pay details.")
    body = alerts_panel + f"""
            <div class="panel">
              <h3>Update Staff Details (Type, Joining Date, Salary, ICAP Reg Date)</h3>
              <p class="note">{approval_note}</p>
              <p style="font-size:12.5px; color:#64748b; margin:0 0 12px 0;">
                EOBI rules: <b>Permanent</b> — starts {eobi_wait} months after joining &nbsp;|&nbsp;
                <b>ICAP Articleship</b> — starts from the date ICAP confirms registration &nbsp;|&nbsp;
                <b>Internee</b> — no EOBI. Once an <b>EOBI Registration Date</b> is entered, EOBI is charged from that date.
                Leave any field blank to keep its current value.
              </p>
              <form class="inline" method="POST" action="/update_staff?ym={ym}">
                <label>Emp ID <input name="emp_id" placeholder="E001" required size="6"></label>
                <label>Staff Type <select name="staff_type"><option value="">(keep current)</option>{type_options}</select></label>
                <label>Date of Joining (Firm) <input type="date" name="date_of_joining"></label>
                <label>Monthly Salary (full-time rate) <input type="number" name="monthly_salary" placeholder="Leave blank to keep"></label>
                <label>Weekly Hours <input type="number" step="0.5" name="weekly_hours" placeholder="{std_week_hours:g} = full time" style="width:120px;"></label>
                <label>ICAP Card Received <input type="date" name="icap_reg_confirmed_date"></label>
                <label>ICAP CRN <input name="icap_crn" size="8"></label>
                <label>ICAP Training Organisation <select name="training_org"><option value="">(keep current)</option>{training_options}</select></label>
                <label>Residual Leaves (NOC) <input type="number" step="0.5" min="0" name="leave_entitlement_override" style="width:90px;"></label>
                <label>Residual Period Ends (NOC) <input type="date" name="articleship_end_date"></label>
                <label>EOBI Registration Date <input type="date" name="eobi_registration_date"></label>
                <label>Status <select name="status"><option value="">(keep current)</option><option value="Active">Active</option><option value="Left">Left</option></select></label>
                <label>Leaving Date <input type="date" name="leaving_date"></label>
                <label>EOBI No. <input name="eobi_no" size="12"></label>
                <label>Date of Birth <input type="date" name="date_of_birth"></label>
                <label>CNIC Issue Date <input type="date" name="cnic_issue_date"></label>
                <label>CNIC Expiry <input type="date" name="cnic_expiry"></label>
                <button class="btn" type="submit">{'Save Staff' if is_approver else 'Submit for Approval'}</button>
              </form>
            </div>
            <div class="panel">
              <h3>Add New Staff Member</h3>
              <p class="note">A new person is never added to the payroll without the approver's approval. {approval_note}
                <b>Documents marked * are mandatory</b> (PDF, JPG or PNG, up to 10 MB each); the ICAP student card only for ICAP students.
                EOBI-only people (not employees) need no documents.</p>
              <form class="inline" method="POST" action="/add_staff?ym={ym}" enctype="multipart/form-data">
                <label>Emp ID <input name="emp_id" placeholder="E018" required size="6"></label>
                <label>Name <input name="employee_name" required></label>
                <label>CNIC <input name="cnic" placeholder="35202-0000000-0"></label>
                <label>CNIC Issue Date <input type="date" name="cnic_issue_date"></label>
                <label>CNIC Expiry * <input type="date" name="cnic_expiry"></label>
                <label>Designation <input name="designation"></label>
                <label>Staff Type <select name="staff_type" required>{type_options}</select></label>
                <label>Date of Joining (Firm) <input type="date" name="date_of_joining"></label>
                <label>Monthly Salary (full-time rate) <input type="number" name="monthly_salary" placeholder="Blank for EOBI Only"></label>
                <label>EOBI No. <input name="eobi_no" size="12"></label>
                <label>Date of Birth <input type="date" name="date_of_birth"></label>
                <label>EOBI Registration Date <input type="date" name="eobi_registration_date"></label>
                <label>ICAP CRN <input name="icap_crn" size="8"></label>
                <label>ICAP Card Received <input type="date" name="icap_reg_confirmed_date"></label>
                <label>ICAP Training Organisation (ICAP / NOC only) <select name="training_org">{training_options}</select></label>
                <label>Residual Leaves (NOC only) <input type="number" step="0.5" min="0" name="leave_entitlement_override" style="width:90px;"></label>
                <label>Residual Period Ends (NOC only) <input type="date" name="articleship_end_date"></label>
                <label>Weekly Hours <input type="number" step="0.5" name="weekly_hours" placeholder="{std_week_hours:g} = full time" style="width:120px;"></label>
                <label>Bank <input name="bank_name"></label>
                <label>IBAN <input name="bank_account_iban"></label>
                <label>Account Title <input name="account_title"></label>
                {new_doc_inputs}
                <button class="btn" type="submit">{'Add Staff Member' if is_approver else 'Submit for Approval'}</button>
              </form>
            </div>
            {docs_panel}
            {types_panel}
            <div class="panel">
              <table>
                <thead><tr><th>ID</th><th>Name</th><th>CNIC</th><th>CNIC Expiry</th><th>Type</th><th>Date of Joining (Firm)</th><th>EOBI Registration Date</th><th>EOBI No.</th><th>EOBI</th><th>ICAP Card Received</th><th>ICAP CRN</th><th>ICAP Training Organisation</th><th class="num">Hours/Week</th><th class="num">Salary</th><th>Bank</th><th>IBAN</th><th class="num">Cum. Leaves</th><th class="num">Withheld</th><th>Status</th><th>Documents</th></tr></thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>"""
    return body
