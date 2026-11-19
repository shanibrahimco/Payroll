"""The holidays tab of the web app."""

import html as html_lib

from auth.roles import ROLE_ACTIONS
from common.constants import EOBI_ONLY_TYPE
from common.dates import parse_date

def render(self, conn, pm_str, role, user, ym, body=""):
    esc = html_lib.escape
    manage = role in ROLE_ACTIONS["/add_holiday"]
    year = parse_date(pm_str).year
    hols = conn.execute("SELECT * FROM holidays WHERE holiday_date BETWEEN ? AND ? ORDER BY holiday_date",
                        (f"{year}-01-01", f"{year + 1}-12-31")).fetchall()
    hol_rows = "".join(
        f"<tr><td>{parse_date(h['holiday_date']).strftime('%a %d %b %Y')}</td><td><strong>{esc(h['name'])}</strong></td>"
        + (f"<td><form method='POST' action='/delete_holiday?ym={ym}'><input type='hidden' name='holiday_date' value='{h['holiday_date']}'>"
           f"<button class='btn btn-danger' type='submit'>Remove</button></form></td>" if manage else "") + "</tr>" for h in hols)
    mail_notes = ""
    if manage:
        no_email = conn.execute("""SELECT employee_name FROM staff WHERE status = 'Active' AND COALESCE(email, '') = ''
                                           AND COALESCE(staff_type, '') != ? ORDER BY emp_id""", (EOBI_ONLY_TYPE,)).fetchall()
        if conn.execute("SELECT 1 FROM mail_accounts WHERE username = ?", (user,)).fetchone() is None:
            mail_notes += "<p class='note'><span class='badge b-warn'>Your sending email account is not set up</span> Holidays will be saved but not emailed until you set it up on the Email tab.</p>"
        if no_email:
            mail_notes += f"<p class='note'><span class='badge b-warn'>No email address, so not emailed:</span> {esc(', '.join(n['employee_name'] for n in no_email))}</p>"
    hol_form = f"""
              <form class="inline" method="POST" action="/add_holiday?ym={ym}">
                <label>Date <input type="date" name="holiday_date" required></label>
                <label>Holiday <input name="name" required size="28" placeholder="e.g. Quaid-e-Azam Day"></label>
                <button class="btn" type="submit">Save and Email Staff</button>
              </form>
              <form method="POST" action="/email_holidays?ym={ym}" style="margin-bottom:12px;">
                <button class="btn btn-sec" type="submit">Email Holiday List Again</button>
                <span class="note">&nbsp; Adding or removing an upcoming holiday emails the updated list to all staff from your own email account.</span>
              </form>{mail_notes}""" if manage else ""
    body = f"""
            <div class="panel">
              <h3>Gazetted Holidays {year}–{year + 1}</h3>
              <p class="note"><b>Saturday and Sunday are always off.</b> Gazetted holidays show automatically as holidays on timesheets and are not counted as absences or late arrivals.
                Staff working at a client follow the client's holidays: an ICAP student whose client didn't allow a gazetted holiday marks
                that day as <b>Work</b> on the timesheet and applies for credit leave on the Leave Register within 7 days.</p>
              {hol_form}
              <table><thead><tr><th>Date</th><th>Holiday</th>{'<th></th>' if manage else ''}</tr></thead>
                <tbody>{hol_rows or "<tr><td colspan='3'>No holidays entered yet.</td></tr>"}</tbody></table>
            </div>"""
    return body
