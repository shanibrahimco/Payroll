"""Sends each web request to the right page or action."""

from datetime import date
from urllib.parse import parse_qs
from urllib.parse import urlparse
import html as html_lib
import sqlite3

import config
from auth.roles import ROLE_ACTIONS, ROLE_APPROVER, ROLE_PREPARER, ROLE_REVIEWER, ROLE_STAFF, ROLE_TABS
from auth.sessions import approver_only_emps, delegated_portals, set_session_acting
from auth.users import user_count
from common.dates import parse_date, pay_month_label
from common.errors import ChangeError
from common.messages import FLASH_MESSAGES
from common.settings import get_settings_dict, is_month_locked
from database import get_conn
from leaves.history_import import build_leave_template
from payroll.bank_files import generate_bank_files
from payroll.calculator import calculate_payroll, recalc_open_months
from payroll.sheet_import import build_payroll_template
from staff.rules import cnic_alert
from timesheets.rules import timesheet_start
from travel.claims import chargeable_to_invoice
from travel.fuel import travel_rate_on
from web.templates import HTML_TEMPLATE

from web.pages import account as account_page
from web.pages import approvals as approvals_page
from web.pages import attendance as attendance_page
from web.pages import holidays as holidays_page
from web.pages import inputs as inputs_page
from web.pages import leaves as leaves_page
from web.pages import loans as loans_page
from web.pages import mail as mail_page
from web.pages import payroll as payroll_page
from web.pages import staff as staff_page
from web.pages import timesheets as timesheets_page
from web.pages import travel as travel_page
from web.pages import users as users_page


class RouterMixin:
    def do_GET(self):
        if urlparse(self.path).path.startswith("/api/"):
            return self.handle_api("GET", urlparse(self.path))
        parsed = urlparse(self.path)
        if parsed.path in ("/login", "/setup"):
            return self.handle_auth_get(parsed.path)
        real_user = self.current_user()
        if not real_user:
            return self.redirect("/setup" if user_count() == 0 else "/login")
        account, user, acting = self.effective_account(real_user)
        role, own_emp = account["role"], account["emp_id"]
        is_approver = role == ROLE_APPROVER
        is_staff = role == ROLE_STAFF
        allowed_tabs = ROLE_TABS.get(role, ())

        qs = parse_qs(parsed.query)
        ym = qs.get("ym", ["2026-11"])[0]
        if len(ym) != 7 or not parse_date(f"{ym}-01"):
            ym = "2026-11"
        tab = qs.get("tab", [allowed_tabs[0] if allowed_tabs else "account"])[0]
        pm_str = f"{ym}-01"
        flash_style, flash_text = FLASH_MESSAGES.get(qs.get("msg", [""])[0], ("", ""))
        conn = get_conn()
        km_rate = travel_rate_on(conn, date.today())  # from today's PSO Premier Euro 5 price
        own_row = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (own_emp,)).fetchone() if own_emp else None
        own_timesheets = own_row is not None and timesheet_start(own_row, get_settings_dict(conn)) is not None
        my_portals = delegated_portals(conn, real_user)
        conn.close()
        if acting:
            portal_bar = (f"<div class='flash flash-warn' style='display:flex; justify-content:space-between; align-items:center; gap:10px;'>"
                          f"<span>You are operating <b>{html_lib.escape(acting)}</b>'s portal. Everything you do is recorded as done by "
                          f"<b>{html_lib.escape(real_user)}</b> for them; their requests are decided by the approver.</span>"
                          f"<form method='POST' action='/stop_acting'><button class='btn btn-sec' type='submit'>Return to My Portal</button></form></div>")
        elif my_portals:
            portal_bar = ("<div class='panel' style='display:flex; gap:8px; align-items:center; flex-wrap:wrap; padding:10px 14px;'>"
                          "<span class='note' style='margin:0;'>Portals you operate:</span>" + "".join(
                              f"<form method='POST' action='/act_as'><input type='hidden' name='emp_id' value='{p['emp_id']}'>"
                              f"<button class='btn btn-sec' type='submit'>Operate {html_lib.escape(p['employee_name'])}'s Portal</button></form>"
                              for p in my_portals) + "</div>")
        else:
            portal_bar = ""
        own_cnic_warn = cnic_alert(own_row["cnic_expiry"]) if own_row is not None else ""
        if own_cnic_warn and (is_staff or acting):
            portal_bar += (f"<div class='flash flash-warn'><b>Your {html_lib.escape(own_cnic_warn)}.</b> "
                           "Please give a copy of your renewed CNIC to the office.</div>")
        if is_staff and not own_timesheets:  # only ICAP students, and staff the reviewer has asked for timesheets
            allowed_tabs = tuple(t for t in allowed_tabs if t != "timesheets")
        if account["must_change_pw"] and not acting:  # starting password set by the approver: nothing else until it is changed
            allowed_tabs = ("account",)
            if tab != "account" or parsed.path != "/":
                return self.redirect(f"/?tab=account&ym={ym}&msg=pw_must_change")

        if parsed.path in ("/timesheet_file", "/timesheet_print"):
            return self.send_timesheet_view(parsed.path, qs, pm_str, role, own_emp)
        if parsed.path == "/staff_doc":
            return self.send_staff_document(qs, role, own_emp)
        if parsed.path == "/travel_receipt":
            return self.send_travel_receipt(qs, role, own_emp)
        if parsed.path == "/leave_template":
            if is_staff or acting:
                return self.redirect("/?msg=not_allowed")
            conn = get_conn()
            content = build_leave_template(conn)
            conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", 'attachment; filename="Leave History Template.xlsx"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path == "/payroll_template":
            if is_staff or acting:
                return self.redirect("/?msg=not_allowed")
            conn = get_conn()
            if conn.execute("SELECT COUNT(*) FROM payroll_lines WHERE payroll_month = ?", (pm_str,)).fetchone()[0] == 0:
                conn.close()
                calculate_payroll(pm_str)
                conn = get_conn()
            content = build_payroll_template(conn, pm_str)
            conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="Payroll Sheet {parse_date(pm_str).strftime("%b %Y")}.xlsx"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if parsed.path not in ("/", "/recalc", "/export") or tab not in allowed_tabs:
            return self.redirect(f"/?tab={allowed_tabs[0]}&ym={ym}")
        if parsed.path == "/recalc" and role not in (ROLE_APPROVER, ROLE_PREPARER):
            return self.redirect(f"/?tab={tab}&ym={ym}&msg=not_allowed")
        if parsed.path == "/export" and is_staff:
            return self.redirect(f"/?tab={tab}&ym={ym}&msg=not_allowed")

        if parsed.path == "/recalc":
            calculate_payroll(pm_str)
            self.send_response(302)
            self.send_header("Location", f"/?tab={tab}&ym={ym}")
            self.end_headers()
            return

        if parsed.path == "/export":
            conn = get_conn()
            locked = is_month_locked(conn, pm_str)
            conn.close()
            if not locked:
                return self.redirect(f"/?tab=payroll&ym={ym}&msg=export_locked")
            ibft_p, ift_p = generate_bank_files(pm_str)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = f"<h3>Bank files generated successfully:</h3><ul><li>{ibft_p}</li><li>{ift_p}</li></ul><a href='/?tab=payroll&ym={ym}'>Back to Payroll</a>"
            self.wfile.write(msg.encode("utf-8"))
            return

        conn = get_conn()
        cnt = conn.execute("SELECT COUNT(*) FROM payroll_lines WHERE payroll_month = ?", (pm_str,)).fetchone()[0]
        if cnt == 0:
            conn.close()
            calculate_payroll(pm_str)
            conn = get_conn()
        pending_count = conn.execute("SELECT COUNT(*) FROM change_requests WHERE status = 'Pending'").fetchone()[0]
        approver_only = approver_only_emps(conn)  # the reviewer's own staff record + portals he operates: decided by the approver
        open_credits = conn.execute("""SELECT emp_id, status FROM leave_credits
                                       WHERE status IN ('Applied', 'Late', 'Recommended') AND source != 'Timesheet'""").fetchall()
        pending_credits = sum(1 for c in open_credits if c["status"] == "Recommended" or c["emp_id"] in approver_only)
        credits_for_review = sum(1 for c in open_credits if c["status"] in ("Applied", "Late") and c["emp_id"] not in approver_only)

        def cannot_decide(emp_id):
            """Own items, and for the reviewer those of staff whose portal he operates."""
            return emp_id == own_emp or (role == ROLE_REVIEWER and emp_id in approver_only)

        open_apps = conn.execute("SELECT emp_id, status FROM loan_applications WHERE status IN ('Applied', 'Recommended')").fetchall()
        loans_for_approver = sum(1 for a in open_apps if a["status"] == "Recommended" or a["emp_id"] in approver_only)
        if not is_staff:
            pending_count += pending_credits + loans_for_approver
        loans_waiting = {ROLE_APPROVER: loans_for_approver,
                         ROLE_REVIEWER: sum(1 for a in open_apps if a["status"] == "Applied" and not cannot_decide(a["emp_id"]))}.get(role, 0)

        body = ""
        if tab == "payroll":
            body = payroll_page.render(self, conn=conn, is_approver=is_approver, pending_count=pending_count, pm_str=pm_str, role=role, ym=ym)
        elif tab == "staff":
            body = staff_page.render(self, conn=conn, is_approver=is_approver, pm_str=pm_str, qs=qs, role=role, ym=ym)
        elif tab == "leaves":
            body = leaves_page.render(self, approver_only=approver_only, cannot_decide=cannot_decide, conn=conn, is_approver=is_approver, is_staff=is_staff, own_emp=own_emp, pm_str=pm_str, role=role, ym=ym)
        elif tab == "holidays":
            body = holidays_page.render(self, conn=conn, pm_str=pm_str, role=role, user=user, ym=ym)
        elif tab == "timesheets":
            body = timesheets_page.render(self, approver_only=approver_only, cannot_decide=cannot_decide, conn=conn, is_staff=is_staff, own_emp=own_emp, pm_str=pm_str, qs=qs, role=role, ym=ym)
        elif tab == "attendance":
            body = attendance_page.render(self, conn=conn, own_emp=own_emp, pm_str=pm_str, role=role, ym=ym)
        elif tab == "email":
            body = mail_page.render(self, account=account, conn=conn, role=role, user=user, ym=ym)
        elif tab == "travel":
            body = travel_page.render(self, cannot_decide=cannot_decide, conn=conn, is_staff=is_staff, km_rate=km_rate, own_emp=own_emp, pm_str=pm_str, role=role, ym=ym)
        elif tab == "loans":
            body = loans_page.render(self, approver_only=approver_only, cannot_decide=cannot_decide, conn=conn, is_approver=is_approver, is_staff=is_staff, own_emp=own_emp, pm_str=pm_str, role=role, ym=ym)
        elif tab == "inputs":
            body = inputs_page.render(self, conn=conn, is_approver=is_approver, pm_str=pm_str, role=role, ym=ym)
        elif tab == "approvals":
            body = approvals_page.render(self, approver_only=approver_only, conn=conn, is_approver=is_approver, ym=ym)
        elif tab == "users":
            body = users_page.render(self, conn=conn, user=user, ym=ym)
        elif tab == "account" and acting:
            body = account_page.render_acting(self, acting=acting)
        elif tab == "account":
            body = account_page.render(self, account=account, conn=conn, flash_text=flash_text, own_emp=own_emp, role=role, user=user, ym=ym)

        popup = ""
        if role == ROLE_REVIEWER:  # reminder to bill clients for chargeable travel once the payroll is approved
            outstanding = chargeable_to_invoice(conn)
            if outstanding:
                total = sum(o["amount"] for o in outstanding)
                key = f"ope-{len(outstanding)}-{total:.0f}"
                items = "".join(f"<tr><td>{html_lib.escape(o['client_name'])}</td><td>{pay_month_label(o['payroll_month'])}</td>"
                                f"<td class='num'>{o['amount']:,.0f}</td></tr>" for o in outstanding)
                popup = f"""
<div id="ope-popup" class="modal-bg"><div class="modal">
  <h3 style="margin-top:0;">Reminder: include chargeable travel in client invoices</h3>
  <p class="note">These out-of-pocket expenses were paid in an approved payroll and are chargeable to the clients below.
    Add them to the invoices, then mark each one on the Travel Claims tab.</p>
  <table><thead><tr><th>Client</th><th>Payroll Month</th><th class="num">Amount (Rs.)</th></tr></thead><tbody>{items}</tbody>
    <tfoot><tr><th colspan="2">Total</th><th class="num">{total:,.0f}</th></tr></tfoot></table>
  <div style="display:flex; gap:8px; margin-top:14px; justify-content:flex-end;">
    <button class="btn btn-sec" type="button" onclick="dismissOpe()">Remind me later</button>
    <a class="btn btn-ok" href="/?tab=travel&ym={ym}#chargeable">Open Chargeable Report</a>
  </div>
</div></div>
<script>
  function dismissOpe() {{ try {{ sessionStorage.setItem("{key}", "1"); }} catch (e) {{}} document.getElementById("ope-popup").remove(); }}
  try {{ if (sessionStorage.getItem("{key}") || location.hash === "#chargeable") document.getElementById("ope-popup").remove(); }} catch (e) {{}}
</script>"""
        claims_to_review = (sum(1 for r in conn.execute("SELECT emp_id FROM travel_claims WHERE status = 'Submitted'") if not cannot_decide(r["emp_id"]))
                            if role in ROLE_ACTIONS["/review_travel"] else 0)
        conn.close()
        tab_labels = {"payroll": "Monthly Payroll", "staff": "Staff Master",
                      "leaves": f"Leave Register{f' ({credits_for_review})' if role == ROLE_REVIEWER and credits_for_review else ''}", "holidays": "Gazetted Holidays", "timesheets": "Timesheets", "email": "Email", "attendance": "Attendance",
                      "travel": f"Travel Claims (Rs. {km_rate:g}/km){f' ({claims_to_review})' if claims_to_review else ''}",
                      "loans": f"Advances & Loans{f' ({loans_waiting})' if loans_waiting else ''}", "inputs": "Monthly Inputs",
                      "approvals": f"Approvals{f' ({pending_count})' if pending_count else ''}", "users": "Users", "account": "My Account"}
        nav_links = "\n  ".join(f'<a href="/?tab={t}&ym={ym}" class="{"active" if t == tab else ""}">{tab_labels[t]}</a>' for t in allowed_tabs)
        header_actions = ""
        if role in (ROLE_APPROVER, ROLE_PREPARER):
            header_actions += f'<a class="btn" href="/recalc?ym={ym}&tab={tab}">Recalculate</a>'
        if not is_staff:
            header_actions += f' <a class="btn btn-sec" href="/export?ym={ym}">Export Bank CSVs</a>'
        html = HTML_TEMPLATE.format(
            firm_name=html_lib.escape(config.FIRM_NAME), firm_short=html_lib.escape(config.FIRM_SHORT), tab=tab, ym=ym, body=body, user=html_lib.escape(user), role=role, km_rate=f"{km_rate:g}",
            flash=portal_bar + (f'<div class="flash flash-{flash_style}">{flash_text}</div>' if flash_text else ""),
            nav_links=nav_links, header_actions=header_actions, popup=popup,
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self.handle_api("POST", parsed)
        data = self.read_form()  # read the body before any early redirect, or the client sees a reset connection
        if parsed.path in ("/login", "/setup", "/logout"):
            return self.handle_auth_post(parsed.path, data)
        real_user = self.current_user()
        if not real_user:
            return self.redirect("/login")
        if parsed.path in ("/act_as", "/stop_acting"):  # switch into / out of a portal delegated to this user
            emp_id = data.get("emp_id", "").strip().upper()
            conn = get_conn()
            allowed = parsed.path == "/stop_acting" or any(p["emp_id"] == emp_id for p in delegated_portals(conn, real_user))
            conn.close()
            if not allowed:
                return self.redirect("/?msg=not_allowed")
            set_session_acting(self.session_token(), emp_id if parsed.path == "/act_as" else None)
            return self.redirect("/?tab=leaves" if parsed.path == "/act_as" else "/")
        account, user, acting = self.effective_account(real_user)
        role = account["role"]
        if acting and parsed.path == "/change_password":
            return self.redirect("/?tab=account&msg=not_allowed")

        qs = parse_qs(parsed.query)
        ym = qs.get("ym", ["2026-11"])[0]
        if len(ym) != 7 or not parse_date(f"{ym}-01"):
            ym = "2026-11"
        pm_str = f"{ym}-01"
        emp_param = "".join(c for c in qs.get("emp", [""])[0].upper() if c.isalnum())
        if emp_param:
            data["emp_id"] = emp_param

        next_tab = {"/upload_emails": "email", "/set_email": "email", "/save_mail_account": "email", "/send_test_email": "email",
                    "/send_logins": "email", "/set_app_url": "email", "/set_approver_email": "email",
                    "/add_holiday": "holidays", "/delete_holiday": "holidays", "/email_holidays": "holidays",
                    "/add_credit": "leaves", "/decide_credit": "leaves", "/apply_credit": "leaves", "/review_credit": "leaves",
                    "/apply_loan": "loans", "/withdraw_loan": "loans", "/review_loan": "loans", "/decide_loan": "loans", "/export_loans": "loans",
                    "/save_timesheet": "timesheets", "/decide_timesheet": "timesheets", "/save_ts_code": "timesheets", "/set_ts_access": "timesheets",
                    "/review_travel": "travel", "/withdraw_travel": "travel", "/mark_invoiced": "travel",
                    "/fetch_fuel_prices": "travel", "/add_fuel_price": "travel", "/set_rate_uplift": "travel",
                    "/update_staff": "staff", "/add_staff": "staff", "/add_leave": "leaves", "/decide_leave": "leaves",
                    "/bulk_leave": "leaves", "/set_travel_rate": "travel",
                    "/add_travel": "travel", "/add_loan": "loans", "/add_input": "inputs", "/approve_request": "approvals",
                    "/reject_request": "approvals", "/add_user": "users", "/delete_user": "users",
                    "/set_delegate": "users", "/remove_delegate": "users",
                    "/upload_document": "staff", "/save_doc_type": "staff", "/toggle_doc_type": "staff",
                    "/upload_attendance": "attendance", "/map_attendance": "attendance", "/absence_action": "attendance",
                    "/save_timing": "attendance", "/delete_timing": "attendance", "/save_part_hours": "attendance",
                    "/save_location": "attendance", "/remove_location": "attendance", "/set_mobile_start": "attendance", "/review_punch": "attendance",
                    "/leave_import_upload": "leaves", "/leave_import_apply": "leaves", "/leave_import_discard": "leaves",
                    "/change_password": "account"}.get(parsed.path, "payroll")
        if next_tab not in ROLE_TABS.get(role, ()):
            next_tab = ROLE_TABS.get(role, ("account",))[0]
        if role not in ROLE_ACTIONS.get(parsed.path, ()):
            return self.redirect(f"/?tab={next_tab}&ym={ym}&msg=not_allowed")
        if account["must_change_pw"] and not acting and parsed.path != "/change_password":
            return self.redirect(f"/?tab=account&ym={ym}&msg=pw_must_change")

        conn = get_conn()
        try:
            msg = self.handle_change(conn, parsed.path, data, ym, pm_str, user, role, account["emp_id"])
            conn.commit()
        except (ChangeError, KeyError, ValueError, sqlite3.IntegrityError) as e:
            conn.rollback()
            msg = e.args[0] if isinstance(e, ChangeError) else "bad_input"
        finally:
            conn.close()
        recalc_open_months()
        emp_back = f"&emp={emp_param}" if next_tab in ("timesheets", "staff") and emp_param and role != ROLE_STAFF else ""
        self.redirect(f"/?tab={next_tab}&ym={ym}{emp_back}&msg={msg}" + ("#docs" if parsed.path == "/upload_document" else ""))
