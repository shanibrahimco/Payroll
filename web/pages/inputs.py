"""The inputs tab of the web app."""

from auth.roles import ROLE_PREPARER

def render(self, conn, is_approver, pm_str, role, ym, body=""):
    mis = conn.execute("SELECT m.*, s.employee_name FROM monthly_inputs m JOIN staff s ON m.emp_id=s.emp_id WHERE m.payroll_month=? ORDER BY m.emp_id", (pm_str,)).fetchall()
    rows_html = "".join(f"""
                <tr>
                  <td>{m['emp_id']}</td><td>{m['employee_name']}</td><td class="num">{m['bonus']:,.0f}</td>
                  <td class="num">{m['arrears_other']:,.0f}</td>
                  <td class="num">{m['partial_days_client_deduction']:,.0f}</td><td class="num">{m['leave_days_waived']}</td><td>{m['note'] or ''}</td>
                </tr>""" for m in mis)
    body = f"""
            <div class="panel">
              <h3>Monthly Adjustments for {ym} (Bonus, Arrears, Client Deductions, Waived Leaves)</h3>
              <p class="note">{'Saved immediately and recorded in the approval log.' if is_approver else '<b>Sent to the approver; the adjustment counts only after approval.</b>' if role == ROLE_PREPARER else '<b>View only.</b>'}</p>
              <form class="inline" method="POST" action="/add_input?ym={ym}">
                <label>Emp ID <input name="emp_id" placeholder="E001" required size="6"></label>
                <label>Bonus <input type="number" name="bonus" value="0" style="width:85px;"></label>
                <label>Arrears <input type="number" name="arrears_other" value="0" style="width:85px;"></label>
                <label>Client Ded. <input type="number" name="partial_days_client_deduction" value="0" style="width:85px;"></label>
                <label>Waived Leaves <input type="number" step="0.5" name="leave_days_waived" value="0" style="width:85px;"></label>
                <label>Note <input name="note" placeholder="Approved by partner"></label>
                <button class="btn" type="submit">Save Input</button>
              </form>
              <table>
                <thead><tr><th>ID</th><th>Name</th><th class="num">Bonus</th><th class="num">Arrears</th><th class="num">Client Ded.</th><th class="num">Waived Lvs</th><th>Note</th></tr></thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>"""
    return body
