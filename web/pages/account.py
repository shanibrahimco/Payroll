"""The account tab of the web app."""

import html as html_lib

from common.constants import MIN_PASSWORD_LEN
from common.messages import FLASH_MESSAGES

def render_acting(self, acting, body=""):
    body = f"""<div class="panel" style="max-width:520px;"><h3>My Account</h3>
              <p class="note">You are operating <b>{html_lib.escape(acting)}</b>'s portal. Their password can't be changed from here.</p></div>"""
    return body


def render(self, account, conn, flash_text, own_emp, role, user, ym, body=""):
    linked = conn.execute("SELECT employee_name FROM staff WHERE emp_id = ?", (own_emp,)).fetchone() if own_emp else None
    body = f"""
            <div class="panel" style="max-width:520px;">
              <h3>My Account</h3>
              <p class="note">Signed in as <b>{html_lib.escape(user)}</b> — {role}{f" — {own_emp} {html_lib.escape(linked['employee_name'])}" if linked else ""}</p>
              {f"<div class='flash flash-warn'>{FLASH_MESSAGES['pw_must_change'][1]}</div>" if account['must_change_pw'] and not flash_text else ""}
              <form method="POST" action="/change_password?ym={ym}" style="display:flex; flex-direction:column; gap:12px; max-width:300px;">
                <label>Current Password <input type="password" name="current" required autocomplete="current-password"></label>
                <label>New Password <input type="password" name="new" required minlength="{MIN_PASSWORD_LEN}" autocomplete="new-password"></label>
                <label>Confirm New Password <input type="password" name="confirm" required autocomplete="new-password"></label>
                <button class="btn" type="submit">Change Password</button>
              </form>
            </div>"""
    return body
