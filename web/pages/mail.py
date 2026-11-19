"""The mail tab of the web app."""

import html as html_lib
import json

import config
from auth.roles import ROLE_ACTIONS
from common.constants import EOBI_ONLY_TYPE
from common.settings import get_settings_dict
from notifications.mailer import MAIL_SECURITY, login_email_text

def render(self, account, conn, role, user, ym, body=""):
    esc = html_lib.escape
    cfg = get_settings_dict(conn)
    app_url = cfg.get("app_url", f"http://127.0.0.1:{config.PORT}")
    people = conn.execute("""
                SELECT s.emp_id, s.employee_name, s.email, u.username, u.must_change_pw,
                       (SELECT MAX(sent_at) FROM email_log l WHERE l.emp_id = s.emp_id AND l.purpose = 'Login details' AND l.status = 'Sent') AS last_sent
                FROM staff s LEFT JOIN users u ON u.emp_id = s.emp_id
                WHERE s.status = 'Active' AND COALESCE(s.staff_type, '') != ? ORDER BY s.emp_id
            """, (EOBI_ONLY_TYPE,)).fetchall()
    account = conn.execute("SELECT * FROM mail_accounts WHERE username = ?", (user,)).fetchone()
    sendable = [p for p in people if p["username"] and p["must_change_pw"] and p["email"]]

    def login_cell(p):
        if not p["username"]:
            return "No login"
        if not p["must_change_pw"]:
            return "<span class='badge b-ok'>Using own password</span>"
        return "<span class='badge b-warn'>Starting password</span>"

    def send_cell(p):
        if not (p["username"] and p["must_change_pw"] and p["email"]):
            return ""
        return (f"<form method='POST' action='/send_logins?ym={ym}'><input type='hidden' name='emp_id' value='{p['emp_id']}'>"
                f"<button class='btn btn-sec' type='submit'{'' if account else ' disabled'}>Send Login</button></form>")

    people_rows = "".join(f"""
                <tr><td>{p['emp_id']}</td><td><strong>{esc(p['employee_name'])}</strong></td><td>{esc(p['username'] or '')}</td>
                  <td>{esc(p['email'] or '') or "<span class='badge b-warn'>No email</span>"}</td><td>{login_cell(p)}</td>
                  <td>{(p['last_sent'] or '')[:16].replace('T', ' ')}</td><td>{send_cell(p)}</td></tr>""" for p in people)

    report_html = ""
    if cfg.get("last_email_upload"):
        rep = json.loads(cfg["last_email_upload"])
        lst = lambda items: "<br>".join(esc(i) for i in items) or "—"
        report_html = f"""<div class="note" style="margin-top:10px;">Last upload: <b>{esc(rep.get('file', ''))}</b> by {esc(rep.get('by', ''))}
                  on {rep.get('at', '')[:16].replace('T', ' ')} — <b>{len(rep['updated'])}</b> updated,
                  <b>{len(rep['unmatched'])}</b> not matched, <b>{len(rep['invalid'])}</b> invalid.
                  {f"<br><b>Not matched to any staff member:</b><br>{lst(rep['unmatched'])}" if rep['unmatched'] else ""}
                  {f"<br><b>Invalid email:</b><br>{lst(rep['invalid'])}" if rep['invalid'] else ""}</div>"""

    sec_opts = lambda cur: "".join(f"<option{' selected' if s == cur else ''}>{s}</option>" for s in MAIL_SECURITY)
    a = account
    account_state = (f"<span class='badge b-ok'>Connected: {esc(a['from_address'])} via {esc(a['smtp_host'])}:{a['smtp_port']}</span>"
                     if a else "<span class='badge b-warn'>Not set up yet</span>")
    log = conn.execute("SELECT * FROM email_log ORDER BY id DESC LIMIT 60").fetchall()
    names = {p["emp_id"]: p["employee_name"] for p in people}
    log_rows = "".join(f"""
                <tr><td>{l['sent_at'][:16].replace('T', ' ')}</td><td>{esc(l['sent_by'])}</td><td>{esc(l['from_address'] or '')}</td>
                  <td>{esc(l['to_address'] or '')}{f" ({esc(names.get(l['emp_id'], l['emp_id']))})" if l['emp_id'] else ''}</td><td>{l['purpose']}</td>
                  <td><span class='badge {'b-ok' if l['status'] == 'Sent' else 'b-warn'}'>{l['status']}</span></td>
                  <td style="white-space:normal; max-width:360px;">{esc(l['error'] or '')}</td></tr>""" for l in log)
    preview = login_email_text("<Name>", "<CNIC without dashes>", "<Starting password>", app_url)

    body = f"""
            <div class="panel">
              <h3>Staff Email Addresses</h3>
              <p class="note">Upload the email list as <b>CSV or Excel (.xlsx)</b>. It needs an <b>Email</b> column and one of
                <b>Emp ID</b>, <b>CNIC</b> or <b>Name</b> to match each row to a staff member. Uploading again updates the addresses.</p>
              <form class="inline" method="POST" action="/upload_emails?ym={ym}" enctype="multipart/form-data">
                <label>Email List <input type="file" name="email_list" accept=".csv,.xlsx" required></label>
                <button class="btn" type="submit">Upload List</button>
              </form>
              <form class="inline" method="POST" action="/set_email?ym={ym}">
                <label>Emp ID <input name="emp_id" required size="6" placeholder="E005"></label>
                <label>Email <input type="email" name="email" size="30" placeholder="name@example.com"></label>
                <button class="btn btn-sec" type="submit">Save One Address</button>
              </form>
              {report_html}
            </div>
            <div class="panel">
              <h3>My Sending Email Account &nbsp; {account_state}</h3>
              <p class="note">Emails go out <b>from the account of whoever sends them</b> (preparer, reviewer or approver). For <b>Gmail</b> use
                server smtp.gmail.com, port 587, STARTTLS and a Google <b>App Password</b> (Google Account → Security → 2-Step Verification →
                App passwords), not your normal password. For <b>Outlook / Microsoft 365</b>: smtp.office365.com, 587, STARTTLS.
                The password is kept only on this computer and is never shown again; leave it blank to keep the saved one.</p>
              <form class="inline" method="POST" action="/save_mail_account?ym={ym}">
                <label>Your Email <input type="email" name="from_address" value="{esc(a['from_address']) if a else ''}" required size="26"></label>
                <label>Display Name <input name="from_name" value="{esc(a['from_name'] or '') if a else esc(config.FIRM_SHORT)}" size="22"></label>
                <label>SMTP Server <input name="smtp_host" value="{esc(a['smtp_host']) if a else 'smtp.gmail.com'}" required size="18"></label>
                <label>Port <input type="number" name="smtp_port" value="{a['smtp_port'] if a else 587}" required style="width:80px;"></label>
                <label>Security <select name="security">{sec_opts(a['security'] if a else 'STARTTLS')}</select></label>
                <label>Login (if different) <input name="smtp_user" value="{esc(a['smtp_user'] or '') if a and a['smtp_user'] != a['from_address'] else ''}" size="18"></label>
                <label>App Password <input type="password" name="smtp_password" autocomplete="new-password" placeholder="{'(saved)' if a and a['smtp_password'] else ''}" size="16"></label>
                <button class="btn" type="submit">Save Account</button>
              </form>
              {f"<form method='POST' action='/send_test_email?ym={ym}'><button class='btn btn-sec' type='submit'>Send Test Email to Myself</button></form>" if a else ""}
            </div>
            <div class="panel">
              <h3>Send Login Details</h3>
              <p class="note">Sends each person their <b>user ID</b> (CNIC without dashes), <b>starting password</b> and the payroll address.
                Only people who <b>haven't set their own password yet</b> can be sent one: passwords are stored scrambled, so the system can only
                send the starting password (it is reset to the starting password at that moment). Everyone must change it at first login.</p>
              <form class="inline" method="POST" action="/set_app_url?ym={ym}">
                <label>Payroll Address in Emails <input name="app_url" value="{esc(app_url)}" size="34" required></label>
                <button class="btn btn-sec" type="submit">Save Address</button>
              </form>
              {f'''<form class="inline" method="POST" action="/set_approver_email?ym={ym}">
                <label>Approver's Email (notified when a leave credit awaits approval) <input type="email" name="approver_email" value="{esc(cfg.get('approver_notify_email', ''))}" size="30"></label>
                <button class="btn btn-sec" type="submit">Save</button>
              </form>''' if role in ROLE_ACTIONS["/set_approver_email"] else ""}
              <p class="note">Set this to the office address once the system is deployed (now it is only reachable on this computer).</p>
              <form method="POST" action="/send_logins?ym={ym}" style="margin-bottom:14px;">
                <button class="btn btn-ok" type="submit"{'' if account and sendable else ' disabled'}>Send Login Details to All ({len(sendable)})</button>
                {"" if account else "<span class='note'>&nbsp; Set up your sending email account first.</span>"}
              </form>
              <details><summary style="cursor:pointer;">Preview of the email</summary><pre style="background:#f8fafc; padding:12px; border-radius:6px;">{esc(preview)}</pre></details>
              <table style="margin-top:12px;">
                <thead><tr><th>ID</th><th>Name</th><th>User ID</th><th>Email</th><th>Login</th><th>Login Email Sent</th><th></th></tr></thead>
                <tbody>{people_rows}</tbody>
              </table>
            </div>
            <div class="panel">
              <h3>Email Log</h3>
              <table>
                <thead><tr><th>When</th><th>Sent By</th><th>From</th><th>To</th><th>Purpose</th><th>Status</th><th>Reason (if failed)</th></tr></thead>
                <tbody>{log_rows or "<tr><td colspan='7'>No emails sent yet.</td></tr>"}</tbody>
              </table>
            </div>"""
    return body
