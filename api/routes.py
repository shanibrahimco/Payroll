"""The mobile app API (/api/*)."""

from datetime import date
from urllib.parse import parse_qs
from urllib.parse import urlparse
import json
import sqlite3
import traceback

from api.helpers import (
    API_ERROR_STATUS,
    API_ROUTES,
    API_WRITES,
    MAX_JSON_BYTES,
    api_check_numbers,
    api_clean_leave,
    api_fields,
    api_loan_limits,
    api_month,
    api_profile,
    api_staff_row,
    api_today_punch,
)
from attendance.mobile import record_punch
from attendance.records import attendance_summary, month_holidays
from attendance.timing import arrived_late, late_arrival_days, staff_timing_rows, timing_on
from auth.roles import MOBILE_ROLES, ROLE_STAFF
from auth.sessions import create_session, delete_session
from auth.users import get_user, normalise_username, try_login
from common.constants import STAFF_LEAVE_TYPES
from common.dates import first_of_month, fmt_date, month_days, parse_date
from common.errors import ChangeError
from common.messages import FLASH_MESSAGES
from common.settings import get_settings_dict, is_month_locked
from database import get_conn
from loans.rules import loan_balance, loan_repaid
from payroll.calculator import held_balance, recalc_open_months
from travel.claims import TRAVEL_MODES
from travel.fuel import petrol_price_on, travel_rate_on

class ApiMixin:
    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        """The request body as a JSON object; anything else is bad input."""
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_JSON_BYTES:
            while length > 0:  # drain it so the client gets the reply
                length -= len(self.rfile.read(min(length, 1 << 20)) or b"x" * length)
            raise ChangeError("bad_input")
        body = self.rfile.read(length) if length > 0 else b""
        data = json.loads(body.decode("utf-8")) if body else {}
        if not isinstance(data, dict):
            raise ChangeError("bad_input")
        return data

    def api_reply(self, code="ok", data=None, top=None):
        payload = {"ok": True, "code": code, "message": FLASH_MESSAGES.get(code, ("", ""))[1], "data": data}
        payload.update(top or {})
        self.send_json(payload)

    def do_OPTIONS(self):
        if not urlparse(self.path).path.startswith("/api/"):
            return self.send_error(404)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_cors_headers()
        self.end_headers()

    def handle_api(self, method, parsed):
        """Every /api/* request: replies are always JSON, {ok, code, message, data}."""
        try:
            data = {}
            if method == "POST":
                if int(self.headers.get("Content-Length", 0)) < 0:
                    raise ChangeError("bad_input")
                data = self.read_form() if parsed.path == "/api/travel" else self.read_json()
            self.api_route(method, parsed.path, parse_qs(parsed.query), data)
        except ChangeError as e:
            self.api_error(e.args[0])
        except (KeyError, ValueError, TypeError, sqlite3.IntegrityError):  # json errors are ValueErrors too
            self.api_error("bad_input")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.api_error("server_error")

    def api_error(self, code):
        message = FLASH_MESSAGES.get(code, ("", "The request could not be completed."))[1]
        self.send_json({"ok": False, "code": code, "message": message, "data": None}, API_ERROR_STATUS.get(code, 400))

    def api_write(self, path, fields, account, recalc=True):
        """One action through handle_change, exactly as the web form would do it."""
        ym, pm_str = api_month({})
        conn = get_conn()
        try:  # on the app everyone acts for themselves only, so a preparer is treated like any other member of staff here
            msg = self.handle_change(conn, path, fields, ym, pm_str, account["username"], ROLE_STAFF, account["emp_id"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if recalc:
            recalc_open_months()
        return msg

    def api_login(self, data):
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ChangeError("bad_input")
        username = normalise_username(username.strip())
        outcome = try_login(username, password, self.client_ip())
        if outcome != "ok":
            raise ChangeError("login_locked" if outcome == "locked" else "login_invalid")
        account = get_user(username)
        if account["role"] not in MOBILE_ROLES:
            raise ChangeError("not_allowed")
        if not account["emp_id"]:
            raise ChangeError("no_emp_link")
        conn = get_conn()
        try:
            staff = api_profile(api_staff_row(conn, account["emp_id"]))
        finally:
            conn.close()
        result = {"token": create_session(username, "mobile"), "must_change_pw": bool(account["must_change_pw"]), "staff": staff}
        self.api_reply("ok", result, result)

    def api_route(self, method, path, qs, data):
        if (method, path) not in API_ROUTES:
            raise ChangeError("not_found")
        if path == "/api/login":
            return self.api_login(data)
        username = self.current_user()
        if not username:
            raise ChangeError("unauthorized")
        if path == "/api/logout":
            token = self.bearer_token()
            if not token:
                raise ChangeError("unauthorized")
            delete_session(token)
            return self.api_reply("logged_out")
        account = get_user(username)
        if account["role"] not in MOBILE_ROLES:
            raise ChangeError("not_allowed")
        if not account["emp_id"]:
            raise ChangeError("no_emp_link")
        if path == "/api/change_password":
            msg = self.api_write("/change_password", api_fields(data), account, recalc=False)
            conn = get_conn()  # a new password signs out every other device
            conn.execute("DELETE FROM sessions WHERE username = ? AND token <> ?", (username, self.bearer_token() or self.session_token() or ""))
            conn.commit()
            conn.close()
            return self.api_reply(msg)
        if account["must_change_pw"]:
            raise ChangeError("pw_must_change")
        return self.api_read(path, qs, account) if method == "GET" else self.api_post(path, data, account)

    def api_post(self, path, data, account):
        emp_id = account["emp_id"]
        if path == "/api/attendance/punch":
            conn = get_conn()
            try:
                msg = record_punch(conn, api_staff_row(conn, emp_id), data.get("type"), data.get("lat"), data.get("lng"), data.get("location_note"))
                conn.commit()
                today = api_today_punch(conn, emp_id)
            finally:
                conn.close()
            if msg != "punched_out":  # the arrival time counts towards late arrivals
                recalc_open_months()
            return self.api_reply(msg, today)
        fields = api_fields(data)
        if path == "/api/leaves/withdraw":
            conn = get_conn()
            try:
                leave = conn.execute("SELECT * FROM leave_register WHERE id = ? AND emp_id = ?", (int(fields["id"]), emp_id)).fetchone()
                if leave is None:
                    raise ChangeError("not_found")
                if leave["status"] != "Pending":
                    raise ChangeError("wrong_leave_stage")
                if is_month_locked(conn, leave["payroll_month"]):
                    raise ChangeError("locked")
                conn.execute("DELETE FROM leave_register WHERE id = ?", (leave["id"],))
                conn.commit()
            finally:
                conn.close()
            recalc_open_months()
            return self.api_reply("leave_withdrawn")
        if path == "/api/leaves":
            api_clean_leave(fields)
        elif path == "/api/travel":
            api_check_numbers(fields, "kms", "parking", "other_charges", "fare")
            claim_date = parse_date(fields.get("claim_date"))
            if claim_date is None:
                raise ChangeError("bad_input")
            fields["claim_date"] = fmt_date(claim_date)
        elif path == "/api/loans":
            api_check_numbers(fields, "amount", "instalments")
        msg = self.api_write(API_WRITES[path], fields, account)
        return self.api_reply("leave_submitted" if path == "/api/leaves" else msg)

    def api_read(self, path, qs, account):
        emp_id = account["emp_id"]
        conn = get_conn()
        try:
            cfg = get_settings_dict(conn)
            staff = api_staff_row(conn, emp_id)
            today = date.today()
            current_month = fmt_date(first_of_month(today))
            rows = lambda sql, *args: [dict(r) for r in conn.execute(sql, args).fetchall()]

            if path == "/api/dashboard":
                _, pm_str = api_month(qs)
                line = conn.execute("SELECT * FROM payroll_lines WHERE payroll_month = ? AND emp_id = ?", (pm_str, emp_id)).fetchone()
                late = late_arrival_days(conn, emp_id, pm_str, cfg)
                petrol_date, petrol_price = petrol_price_on(conn, today)
                leave_keys = ("leave_balance", "leave_limit", "leaves_this_month", "leaves_cumulative", "leave_credit_days", "leave_carried_forward")
                return self.api_reply(data={
                    "staff": api_profile(staff),
                    "leave": {k: line[k] if line else None for k in leave_keys},
                    "late": {"late_count": len(late), "late_hold_days": int(float(cfg.get("late_hold_days", 3))), "late_dates": [d for d, _ in late],
                             "is_salary_held_late": bool(line and float(line["salary_withheld_late"] or 0) > 0)},
                    "loan": api_loan_limits(conn, staff, current_month, cfg),
                    "travel": {"km_rate": travel_rate_on(conn, today, cfg), "petrol_price": petrol_price, "petrol_effective_date": petrol_date,
                               "travel_modes": TRAVEL_MODES},
                    "today": api_today_punch(conn, emp_id)})

            if path == "/api/attendance":
                _, pm_str = api_month(qs)
                days = month_days(pm_str)
                summary = attendance_summary(conn, pm_str).get(emp_id) or {"name": staff["employee_name"], "present": 0, "late": 0, "absent": [],
                                                                           "off": 0, "days": 0, "covered": [], "uncovered": []}
                holidays = month_holidays(conn, pm_str)
                timings = staff_timing_rows(conn, emp_id)
                daily = []
                for r in rows("""SELECT att_date, status, arrival_time, check_in_time, check_out_time, source, location_note, location_status FROM attendance
                                 WHERE emp_id = ? AND att_date BETWEEN ? AND ? ORDER BY att_date""", emp_id, fmt_date(days[0]), fmt_date(days[-1])):
                    timing = timing_on(timings, r["att_date"], cfg)
                    working_day = r["att_date"] not in holidays and parse_date(r["att_date"]).weekday() not in timing["off"]
                    r["is_late"] = bool(working_day and arrived_late(r["status"], r["arrival_time"], cfg, timing))
                    r["is_uncovered_absence"] = r["att_date"] in summary["uncovered"]
                    daily.append(r)
                recorded = {r["att_date"] for r in daily}
                daily += [{"att_date": d, "status": "A", "arrival_time": None, "check_in_time": None, "check_out_time": None, "source": "Mobile",
                           "location_note": None, "location_status": None, "is_late": False, "is_uncovered_absence": d in summary["uncovered"]}
                          for d in summary["absent"] if d not in recorded]  # working days with no check-in
                daily.sort(key=lambda r: r["att_date"])
                return self.api_reply(data={"summary": summary, "days": daily})

            if path == "/api/leaves":
                return self.api_reply(data={
                    "leaves": rows("SELECT * FROM leave_register WHERE emp_id = ? ORDER BY date_from DESC, id DESC", emp_id),
                    "credits": rows("SELECT * FROM leave_credits WHERE emp_id = ? ORDER BY work_date DESC, id DESC", emp_id),
                    "leave_types": STAFF_LEAVE_TYPES, "cutoff_day": int(float(cfg.get("cutoff_day", 25))),
                    "credit_apply_days": int(float(cfg.get("credit_apply_days", 7)))})

            if path == "/api/travel":
                claims = rows("SELECT * FROM travel_claims WHERE emp_id = ? ORDER BY claim_date DESC, id DESC", emp_id)
                for c in claims:
                    c["has_receipt"] = bool(c.pop("receipt_file"))
                return self.api_reply(data={"claims": claims, "km_rate": travel_rate_on(conn, today, cfg), "travel_modes": TRAVEL_MODES})

            if path == "/api/loans":
                as_of = parse_date(current_month)
                active = []
                for ln in rows("SELECT * FROM advances_loans WHERE emp_id = ? ORDER BY date_given DESC", emp_id):
                    ln["repaid_directly"] = loan_repaid(conn, ln["loan_ref"])
                    ln["balance"] = loan_balance(ln, as_of, ln["repaid_directly"])
                    if ln["balance"] > 0:
                        active.append(ln)
                return self.api_reply(data={"applications": rows("SELECT * FROM loan_applications WHERE emp_id = ? ORDER BY id DESC", emp_id),
                                            "active_loans": active, "repayment_methods": ["Instalments", "Lump sum"],
                                            "repayments": rows("SELECT loan_ref, amount, paid_on, note FROM loan_repayments WHERE emp_id = ? ORDER BY paid_on DESC", emp_id),
                                            **api_loan_limits(conn, staff, current_month, cfg)})

            _, pm_str = api_month(qs)  # /api/payslip
            run = conn.execute("SELECT status FROM payroll_runs WHERE payroll_month = ?", (pm_str,)).fetchone()
            line = conn.execute("SELECT * FROM payroll_lines WHERE payroll_month = ? AND emp_id = ?", (pm_str, emp_id)).fetchone()
            if run is None or run["status"] != "Approved" or line is None:
                raise ChangeError("payslip_not_ready")
            return self.api_reply(data={"payslip": dict(line), "held_balance": held_balance(conn, staff, pm_str)})
        finally:
            conn.close()
