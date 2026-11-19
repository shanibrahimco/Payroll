"""The users tab of the web app."""

import html as html_lib

from auth.roles import ROLES, ROLE_STAFF
from auth.users import staff_access_ended
from common.constants import MIN_PASSWORD_LEN

def render(self, conn, user, ym, body=""):
    def access_badge(u):
        if u["emp_id"] is None or u["employee_name"] is None:
            return ""
        if staff_access_ended(u):
            return " <span class='badge b-warn'>Access removed (left the firm)</span>"
        return f" <span class='badge b-warn'>Access ends after {u['leaving_date']}</span>" if u["leaving_date"] else ""

    users = conn.execute("""SELECT u.username, u.role, u.emp_id, u.created_at, u.must_change_pw, s.employee_name, s.status, s.leaving_date FROM users u
                                    LEFT JOIN staff s ON s.emp_id = u.emp_id
                                    ORDER BY CASE u.role WHEN 'Approver' THEN 0 WHEN 'Reviewer' THEN 1 WHEN 'Preparer' THEN 2 ELSE 3 END, u.username""").fetchall()
    rows_html = "".join(f"""
                <tr>
                  <td><strong>{html_lib.escape(u['username'])}</strong></td><td>{u['role']}</td>
                  <td>{(u['emp_id'] or '') + (' — ' + u['employee_name'] if u['employee_name'] else '')}{access_badge(u)}</td><td>{u['created_at'].replace('T', ' ')}</td>
                  <td>{"<span class='badge b-warn'>Starting password not changed yet</span>" if u['must_change_pw'] else "<span class='badge b-ok'>Own password set</span>"}</td>
                  <td>{'' if u['username'] == user else f"<form method='POST' action='/delete_user?ym={ym}'><input type='hidden' name='username' value='{html_lib.escape(u['username'])}'><button class='btn btn-danger' type='submit'>Remove</button></form>"}</td>
                </tr>""" for u in users)
    role_options = "".join(f'<option value="{r}"{" selected" if r == ROLE_STAFF else ""}>{r}</option>' for r in ROLES)
    body = f"""
            <div class="panel">
              <h3>Users</h3>
              <p class="note">
                <b>Approver</b> — approves staff / pay changes and each month's payroll; own changes apply at once.<br>
                <b>Reviewer</b> — sees everything; approves or rejects leave applications; reviews the prepared payroll.<br>
                <b>Preparer</b> — enters data and prepares the payroll; new staff, pay changes, adjustments and loans wait for approval.<br>
                <b>Staff</b> — can only apply for their own leave and submit their own travel claims (must be linked to an Emp ID).<br>
                Give each person a starting password; they must change it to their own the first time they log in.
              </p>
              <form class="inline" method="POST" action="/add_user?ym={ym}">
                <label>Username <input name="username" required autocomplete="off"></label>
                <label>Temporary Password <input type="password" name="password" required minlength="{MIN_PASSWORD_LEN}" autocomplete="new-password"></label>
                <label>Role <select name="role">{role_options}</select></label>
                <label>Emp ID <input name="emp_id" placeholder="E003" size="6"></label>
                <button class="btn" type="submit">Add User</button>
              </form>
              <table>
                <thead><tr><th>Username</th><th>Role</th><th>Staff Member</th><th>Created</th><th>Password</th><th></th></tr></thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>"""
    delegates = conn.execute("""SELECT d.*, s.employee_name, u.role FROM portal_delegates d JOIN staff s ON s.emp_id = d.emp_id
                                        LEFT JOIN users u ON u.username = d.delegate_username ORDER BY d.emp_id""").fetchall()
    staff_logins = conn.execute("""SELECT u.emp_id, s.employee_name FROM users u JOIN staff s ON s.emp_id = u.emp_id
                                           WHERE u.role = ? ORDER BY u.emp_id""", (ROLE_STAFF,)).fetchall()
    officers = [u for u in users if u["role"] != ROLE_STAFF]
    del_rows = "".join(
        f"<tr><td>{d['emp_id']} {html_lib.escape(d['employee_name'])}</td><td>{html_lib.escape(d['delegate_username'])} ({d['role'] or '?'})</td>"
        f"<td>{html_lib.escape(d['granted_by'] or '')} {(d['granted_at'] or '')[:10]}</td>"
        f"<td><form method='POST' action='/remove_delegate?ym={ym}'><input type='hidden' name='emp_id' value='{d['emp_id']}'>"
        f"<button class='btn btn-danger' type='submit'>Remove</button></form></td></tr>" for d in delegates)
    body += f"""
            <div class="panel">
              <h3>Portals Operated for Staff</h3>
              <p class="note">For staff who can't use the portal themselves. The operator switches into the staff member's portal (leave,
                travel, loans, timesheet) from their own login; everything is recorded as done by the operator for that person. The reviewer
                never reviews what he files for them: those requests go straight to the approver.</p>
              <form class="inline" method="POST" action="/set_delegate?ym={ym}">
                <label>Staff Member <select name="emp_id">{"".join(f"<option value='{s['emp_id']}'>{s['emp_id']} — {html_lib.escape(s['employee_name'])}</option>" for s in staff_logins)}</select></label>
                <label>Operated By <select name="delegate_username">{"".join(f"<option value='{html_lib.escape(o['username'])}'>{html_lib.escape(o['username'])} — {o['role']}{(' (' + html_lib.escape(o['employee_name']) + ')') if o['employee_name'] else ''}</option>" for o in officers)}</select></label>
                <button class="btn" type="submit">Assign</button>
              </form>
              <table><thead><tr><th>Staff Member</th><th>Operated By</th><th>Assigned</th><th></th></tr></thead>
                <tbody>{del_rows or "<tr><td colspan='4'>None.</td></tr>"}</tbody></table>
            </div>"""
    return body
