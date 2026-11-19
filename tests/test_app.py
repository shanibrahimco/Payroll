"""End-to-end checks of the web app and mobile API. They run on a temporary COPY of the database and never touch live data.

Run from the project folder:  python tests/test_app.py
"""
import io, json, os, shutil, sqlite3, sys, tempfile, threading, urllib.error, urllib.parse, urllib.request, uuid
from datetime import date, datetime, timedelta
from http.server import HTTPServer
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
from attendance.mobile import distance_metres
from attendance.records import attendance_summary, store_attendance
from auth.roles import ROLE_APPROVER, ROLE_PREPARER, ROLE_REVIEWER, ROLE_STAFF
from auth.users import LOGIN_FAILURES, LOGIN_WINDOW_SECONDS, create_user
from common.constants import ABSENCE_LEAVE_TYPE
from database import get_conn, init_db
from payroll.calculator import calculate_payroll
from web.handler import PayrollHandler

app = SimpleNamespace(init_db=init_db, create_user=create_user, ROLE_STAFF=ROLE_STAFF, ROLE_PREPARER=ROLE_PREPARER, ROLE_REVIEWER=ROLE_REVIEWER,
                      ROLE_APPROVER=ROLE_APPROVER, HTTPServer=HTTPServer, PayrollHandler=PayrollHandler, get_conn=get_conn, store_attendance=store_attendance,
                      attendance_summary=attendance_summary, calculate_payroll=calculate_payroll, LOGIN_FAILURES=LOGIN_FAILURES,
                      LOGIN_WINDOW_SECONDS=LOGIN_WINDOW_SECONDS, datetime=datetime, ABSENCE_LEAVE_TYPE=ABSENCE_LEAVE_TYPE, distance_metres=distance_metres)

work = tempfile.mkdtemp(prefix="payroll_test_")
db = os.path.join(work, "copy.db")
shutil.copy(config.COMPANIES["sic"]["db"], db)
config.COMPANIES["sic"].update(db=db, exports=os.path.join(work, "exports"), uploads=os.path.join(work, "uploads"))
config.use_company("sic")
assert os.path.abspath(config.DB_PATH) == os.path.abspath(db), "not using the copy"
os.makedirs(config.UPLOAD_DIR, exist_ok=True)
app.init_db()

c = sqlite3.connect(db); c.row_factory = sqlite3.Row
emps = [r["emp_id"] for r in c.execute("SELECT emp_id FROM staff WHERE status='Active' AND staff_type IN ('Permanent','ICAP Article') ORDER BY emp_id LIMIT 6")]
c.close()
A, B, L, P, R, Y = emps
app.create_user("t_staff", "Password#1", app.ROLE_STAFF, A, must_change=True)
app.create_user("t_other", "Password#2", app.ROLE_STAFF, B)
app.create_user("t_prep", "Password#3", app.ROLE_PREPARER)
app.create_user("t_appr", "Password#4", app.ROLE_APPROVER)
app.create_user("t_lock", "Password#5", app.ROLE_STAFF, L)
app.create_user("t_prep2", "Password#6", app.ROLE_PREPARER, P)
app.create_user("t_rev2", "Password#7", app.ROLE_REVIEWER, R)
app.create_user("t_appr2", "Password#8", app.ROLE_APPROVER, Y)

srv = app.HTTPServer(("127.0.0.1", 0), app.PayrollHandler)  # any free port
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
results = []
def check(name, cond, extra=""):
    results.append(bool(cond)); print(("PASS" if cond else "FAIL"), name, extra if not cond else "")

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
opener = urllib.request.build_opener(NoRedirect)

def call(method, path, body=None, token=None, raw=None, ctype=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None: req.add_header("Content-Type", "application/json")
    if ctype: req.add_header("Content-Type", ctype)
    if token: req.add_header("Authorization", "Bearer " + token)
    try: r = opener.open(req, timeout=120)
    except urllib.error.HTTPError as e: r = e
    text = r.read().decode("utf-8", "replace")
    try: js = json.loads(text)
    except ValueError: js = None
    return r.status if hasattr(r, "status") else r.code, js, r.headers

def multipart(fields, file_field=None, filename=None, content=b""):
    b = uuid.uuid4().hex; out = io.BytesIO()
    for k, v in fields.items():
        out.write(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    if file_field:
        out.write(f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\nContent-Type: image/png\r\n\r\n'.encode() + content + b"\r\n")
    out.write(f"--{b}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={b}"

def web_login(u, p):
    s, j, h = call("POST", "/login", raw=urllib.parse.urlencode({"username": u, "password": p}).encode(), ctype="application/x-www-form-urlencoded")
    return h["Set-Cookie"].split(";")[0]
def web_post(cookie, path, fields):
    req = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
    try: r = opener.open(req, timeout=120)
    except urllib.error.HTTPError as e: r = e
    return r.headers.get("Location", "")
def web_get(cookie, path):
    r = opener.open(urllib.request.Request(BASE + path, headers={"Cookie": cookie}), timeout=120)
    return r.status, r.read().decode("utf-8")

# ---- auth
s, j, _ = call("POST", "/api/login", {"username": "t_staff", "password": "wrong"}); check("bad password 401", s == 401 and j["code"] == "login_invalid")
s, j, _ = call("POST", "/api/login", {"username": "t_appr2", "password": "Password#8"}); check("approver refused on the app", s == 403 and j["code"] == "not_allowed")
s, j, _ = call("POST", "/api/login", {"username": "t_prep", "password": "Password#3"}); check("preparer with no staff link refused", s == 403 and j["code"] == "no_emp_link")
s, j, _ = call("POST", "/api/login", raw=b"{not json", ctype="application/json"); check("malformed JSON 400", s == 400 and j["code"] == "bad_input")
s, j, _ = call("GET", "/api/dashboard"); check("no token 401", s == 401)
s, j, _ = call("GET", "/api/dashboard", token="nonsense"); check("bad token 401", s == 401)
s, j, _ = call("GET", "/api/nothing"); check("unknown route 404 JSON", s == 404 and j["code"] == "not_found")
s, j, h = call("OPTIONS", "/api/login"); check("CORS preflight", s == 204 and h["Access-Control-Allow-Origin"] == "*" and "Authorization" in h["Access-Control-Allow-Headers"])
s, j, _ = call("POST", "/api/login", {"username": "t_staff", "password": "Password#1"})
check("login", s == 200 and j["ok"] and j["token"] and j["must_change_pw"] is True and j["staff"]["emp_id"] == A, j)
T = j["token"]
c = sqlite3.connect(db); row = c.execute("SELECT client_type, expires_at FROM sessions WHERE token=?", (T,)).fetchone(); c.close()
check("mobile session ~30 days", row[0] == "mobile" and row[1][:10] >= (date.today() + timedelta(days=29)).isoformat(), row)
s, j, _ = call("GET", "/api/dashboard", token=T); check("must change password blocks API", s == 403 and j["code"] == "pw_must_change")
s, j, _ = call("POST", "/api/change_password", {"current": "bad", "new": "NewPassword#1", "confirm": "NewPassword#1"}, T); check("wrong current password", s == 400 and j["code"] == "pw_invalid")
s, j, _ = call("POST", "/api/login", {"username": "t_staff", "password": "Password#1"}); T2 = j["token"]
s, j, _ = call("POST", "/api/change_password", {"current": "Password#1", "new": "NewPassword#1", "confirm": "NewPassword#1"}, T); check("change password", s == 200 and j["code"] == "pw_changed", j)
s, j, _ = call("GET", "/api/dashboard", token=T2); check("other device signed out after password change", s == 401)
s, j, _ = call("POST", "/api/login", {"username": "t_other", "password": "Password#2"}); TB = j["token"]

# ---- login limit
for i in range(5): s, j, _ = call("POST", "/api/login", {"username": "t_lock", "password": "wrong"})
check("5th wrong password still reads as invalid", s == 401)
s, j, _ = call("POST", "/api/login", {"username": "t_lock", "password": "Password#5"}); check("locked even with the right password", s == 429 and j["code"] == "login_locked", j)
s, j, h = call("POST", "/login", raw=b"username=t_lock&password=Password%235", ctype="application/x-www-form-urlencoded"); check("web login locked too", s == 429)
s, j, _ = call("POST", "/api/login", {"username": "t_other", "password": "Password#2"}); check("other users unaffected by the lock", s == 200)
now = app.datetime.now().timestamp()
app.LOGIN_FAILURES["user:t_lock"] = [now - app.LOGIN_WINDOW_SECONDS - 1] * 5
s, j, _ = call("POST", "/api/login", {"username": "t_lock", "password": "Password#5"}); check("login works once the 15 minutes pass", s == 200, j)

# ---- dashboard
s, j, _ = call("GET", "/api/dashboard?ym=2026-11", token=T); d = j["data"]
check("dashboard", s == 200 and d["staff"]["emp_id"] == A and "leave_balance" in d["leave"] and "late_count" in d["late"] and "available" in d["loan"] and d["travel"]["km_rate"] > 0 and d["today"]["checked_in"] is False, j)
s, j, _ = call("GET", "/api/dashboard?ym=garbage", token=T); check("bad month 400", s == 400)

# ---- locations
AP = web_login("t_appr", "Password#4"); PR = web_login("t_prep", "Password#3")
ym = date.today().strftime("%Y-%m"); TODAY = date.today().isoformat()
check("preparer can't add a location", "not_allowed" in web_post(PR, f"/save_location?ym={ym}", {"name": "X", "kind": "Office", "lat": "24.86", "lng": "67.01"}))
check("bad coordinates refused", "location_invalid" in web_post(AP, f"/save_location?ym={ym}", {"name": "X", "kind": "Office", "lat": "999", "lng": "67.01"}))
check("bad radius refused", "location_invalid" in web_post(AP, f"/save_location?ym={ym}", {"name": "X", "kind": "Office", "lat": "24.86", "lng": "67.01", "radius_m": "5"}))
check("approver adds office", "location_saved" in web_post(AP, f"/save_location?ym={ym}", {"name": "Head Office", "kind": "Office", "lat": "24.86", "lng": "67.01", "radius_m": "150"}))
check("distance formula (1 degree latitude is about 111 km)", 110000 < app.distance_metres(24, 67, 25, 67) < 112000)

# ---- punch
s, j, _ = call("POST", "/api/attendance/punch", {"type": "out", "lat": 24.86, "lng": 67.01}, T); check("out before in rejected", s == 400 and j["code"] == "not_checked_in")
s, j, _ = call("POST", "/api/attendance/punch", {"type": "in", "lat": 999, "lng": 67.0}, T); check("bad GPS rejected", s == 400 and j["code"] == "bad_location")
s, j, _ = call("POST", "/api/attendance/punch", {"type": "in", "lat": "x", "lng": 67.0}, T); check("non-numeric GPS rejected", s == 400 and j["code"] == "bad_location")
s, j, _ = call("POST", "/api/attendance/punch", {"type": "in", "lat": 24.8605, "lng": 67.0105, "location_note": "typed by staff"}, T)
check("check-in inside radius verified, site name used", s == 200 and j["code"] == "punched_in" and j["data"]["location_status"] == "Verified" and j["data"]["location_note"] == "Head Office", j)
s, j, _ = call("POST", "/api/attendance/punch", {"type": "in", "lat": 24.86, "lng": 67.01}, T); check("double check-in rejected", s == 400 and j["code"] == "already_checked_in")
s, j, _ = call("POST", "/api/attendance/punch", {"type": "out", "lat": 24.87, "lng": 67.02}, T); check("check out", s == 200 and j["data"]["checked_out"], j)
s, j, _ = call("POST", "/api/attendance/punch", {"type": "out", "lat": 24.87, "lng": 67.02}, T); check("double check-out rejected", s == 400 and j["code"] == "already_checked_out")
s, j, _ = call("GET", f"/api/attendance?ym={ym}", token=T); today = [x for x in j["data"]["days"] if x["att_date"] == TODAY]
check("attendance log shows punch", s == 200 and today and today[0]["source"] == "Mobile" and today[0]["status"] == "P" and today[0]["location_status"] == "Verified", j)
s, j, _ = call("POST", "/api/attendance/punch", {"type": "in", "lat": 31.52, "lng": 74.35}, TB); check("far away with no place named refused", s == 400 and j["code"] == "location_need_note", j)
s, j, _ = call("POST", "/api/attendance/punch", {"type": "in", "lat": 31.52, "lng": 74.35, "location_note": "ABC Pvt Ltd"}, TB)
check("far away with a place named goes to review", s == 200 and j["code"] == "punched_in_review" and j["data"]["location_status"] == "Review", j)
s, html = web_get(AP, f"/?tab=attendance&ym={ym}"); check("attendance tab shows the review and no upload form", s == 200 and "ABC Pvt Ltd" in html and "Head Office" in html and "Upload Attendance" not in html)
s, html = web_get(PR, f"/?tab=attendance&ym={ym}"); check("preparer sees the tab without decide buttons", s == 200 and "ABC Pvt Ltd" in html and "/review_punch" not in html)
check("preparer can't decide", "not_allowed" in web_post(PR, f"/review_punch?ym={ym}", {"emp_id": B, "att_date": TODAY, "decision": "Accepted"}))
check("reject needs a reason", "punch_need_note" in web_post(AP, f"/review_punch?ym={ym}", {"emp_id": B, "att_date": TODAY, "decision": "Rejected"}))
check("reject", "punch_rejected" in web_post(AP, f"/review_punch?ym={ym}", {"emp_id": B, "att_date": TODAY, "decision": "Rejected", "note": "Not at client"}))
check("can't decide twice", "punch_wrong_stage" in web_post(AP, f"/review_punch?ym={ym}", {"emp_id": B, "att_date": TODAY, "decision": "Accepted"}))
s, j, _ = call("GET", f"/api/attendance?ym={ym}", token=TB); r = [x for x in j["data"]["days"] if x["att_date"] == TODAY][0]
check("rejected check-in is an absence", r["status"] == "A" and r["location_status"] == "Rejected" and r["arrival_time"] is None, r)

# a mobile check-in prevails over any uploaded report
conn = app.get_conn()
conn.execute("UPDATE staff SET attendance_code='T99' WHERE emp_id=?", (A,))
app.store_attendance(conn, None, [{"code": "T99", "name": "x", "days": {TODAY: "A"}, "arrivals": {}}]); conn.commit()
r = conn.execute("SELECT * FROM attendance WHERE emp_id=? AND att_date=?", (A, TODAY)).fetchone(); conn.close()
check("report can't override a mobile check-in", r["status"] == "P" and r["source"] == "Mobile" and r["arrival_time"] == r["check_in_time"])

# absences from missing check-ins
past = [date.today() - timedelta(days=i) for i in range(1, 8)]
past = [d for d in past if d.month == date.today().month and d.weekday() < 5]
s, j, _ = call("GET", f"/api/attendance?ym={ym}", token=T); check("no start date: no absences marked", s == 200 and j["data"]["summary"]["absent"] == [], j["data"]["summary"])
check("set start date", "mobile_start_saved" in web_post(AP, f"/set_mobile_start?ym={ym}", {"start_date": past[-1].isoformat()}))
s, j, _ = call("GET", f"/api/attendance?ym={ym}", token=T); ab = j["data"]["summary"]["absent"]
conn = app.get_conn(); hol = {r[0] for r in conn.execute("SELECT holiday_date FROM holidays")}; timed = conn.execute("SELECT COUNT(*) FROM staff_timings WHERE emp_id=?", (A,)).fetchone()[0]; conn.close()
expect = sorted(d.isoformat() for d in past if d.isoformat() not in hol)
check("missed working days listed as absent", timed > 0 or ab == expect, (ab, expect))
check("absent days appear in the daily log", {x["att_date"] for x in j["data"]["days"] if x["status"] == "A"} >= set(ab))
conn = app.get_conn(); conn.execute("INSERT OR REPLACE INTO portal_delegates (emp_id, delegate_username) VALUES (?, 't_prep')", (L,)); conn.commit()
sm = app.attendance_summary(conn, f"{ym}-01"); conn.close()
check("approver-linked person is exempt", Y not in sm, sorted(sm))
check("preparer is marked absent like anyone else", P in sm and sm[P]["absent"], sm.get(P))
check("reviewer is marked absent like anyone else", R in sm and sm[R]["absent"], sm.get(R))
check("person with an operated portal must check in too", L in sm and sm[L]["absent"], sm.get(L))
s, html = web_get(AP, f"/?tab=attendance&ym={ym}"); check("attendance tab renders with absences", s == 200 and "File as Leave" in html)

# reviewer and preparer on the app, under the same rules
s, j, _ = call("POST", "/api/login", {"username": "t_prep2", "password": "Password#6"}); check("preparer logs in on the app", s == 200 and j["staff"]["emp_id"] == P, j); TP = j["token"]
s, j, _ = call("POST", "/api/login", {"username": "t_rev2", "password": "Password#7"}); check("reviewer logs in on the app", s == 200 and j["staff"]["emp_id"] == R, j); TR = j["token"]
s, j, _ = call("POST", "/api/attendance/punch", {"type": "in", "lat": 24.86, "lng": 67.01}, TP); check("preparer checks in", s == 200 and j["data"]["location_status"] == "Verified", j)
fut2 = (date.today() + timedelta(days=4)).isoformat()
s, j, _ = call("POST", "/api/leaves", {"leave_type": "Sick", "date_from": fut2, "date_to": fut2, "no_of_days": 1, "reason": "preparer own", "emp_id": B}, TP); check("preparer applies for leave on the app", s == 200, j)
conn = app.get_conn(); r = conn.execute("SELECT emp_id, status FROM leave_register WHERE reason='preparer own'").fetchone(); conn.close()
check("preparer's app leave is their own, even if another ID is sent", r["emp_id"] == P and r["status"] == "Pending", dict(r))
s, j, _ = call("POST", "/api/attendance/punch", {"type": "in", "lat": 31.52, "lng": 74.35, "location_note": "Client X"}, TR); check("reviewer away from the office goes to review", j["code"] == "punched_in_review", j)
RV = web_login("t_rev2", "Password#7"); PV = web_login("t_prep2", "Password#6")
check("reviewer can't accept own check-in", "punch_own" in web_post(RV, f"/review_punch?ym={ym}", {"emp_id": R, "att_date": TODAY, "decision": "Accepted"}))
s, html = web_get(RV, f"/?tab=attendance&ym={ym}"); check("reviewer sees the approver decides their own", s == 200 and "The approver decides this one" in html)
check("approver accepts reviewer's check-in", "punch_accepted" in web_post(AP, f"/review_punch?ym={ym}", {"emp_id": R, "att_date": TODAY, "decision": "Accepted"}))
check("preparer can't clear own absence", "absence_own" in web_post(PV, f"/absence_action?ym={ym}", {"emp_id": P, "action": "leave", "leave_type": "Sick"}))
check("preparer can still file another person's absence", "absences_filed" in web_post(PV, f"/absence_action?ym={ym}", {"emp_id": L, "action": "leave", "leave_type": "Sick"}))
check("nobody can upload attendance days", "not_allowed" in web_post(PV, f"/upload_attendance?ym={ym}", {}) and "not_allowed" in web_post(AP, f"/upload_attendance?ym={ym}", {}))

# ---- leaves
fut = date.today() + timedelta(days=3)
good = {"application_date": TODAY, "leave_type": "Sick", "date_from": fut.isoformat(), "date_to": fut.isoformat(), "no_of_days": 1, "reason": "API test"}
s, j, _ = call("POST", "/api/leaves", {**good, "leave_type": app.ABSENCE_LEAVE_TYPE}, T); check("staff can't file absence type", s == 400)
s, j, _ = call("POST", "/api/leaves", {**good, "no_of_days": 5}, T); check("more days than the dates rejected", s == 400)
s, j, _ = call("POST", "/api/leaves", {**good, "no_of_days": "nan"}, T); check("NaN days rejected", s == 400)
s, j, _ = call("POST", "/api/leaves", {**good, "date_to": (fut - timedelta(days=1)).isoformat()}, T); check("end before start rejected", s == 400)
s, j, _ = call("POST", "/api/leaves", {**good, "emp_id": B}, T); check("apply for leave", s == 200 and j["code"] == "leave_submitted", j)
s, j, _ = call("GET", "/api/leaves", token=T); mine = [l for l in j["data"]["leaves"] if l["reason"] == "API test"]
check("leave is own and Pending, emp_id spoof ignored", len(mine) == 1 and mine[0]["emp_id"] == A and mine[0]["status"] == "Pending" and app.ABSENCE_LEAVE_TYPE not in j["data"]["leave_types"], j)
lid = mine[0]["id"]
s, j, _ = call("POST", "/api/leaves/withdraw", {"id": lid}, TB); check("can't withdraw someone else's leave", s == 404)
conn = app.get_conn(); conn.execute("UPDATE leave_register SET status='Approved' WHERE id=?", (lid,)); conn.commit()
s, j, _ = call("POST", "/api/leaves/withdraw", {"id": lid}, T); check("can't withdraw approved leave", s == 400 and j["code"] == "wrong_leave_stage")
conn.execute("UPDATE leave_register SET status='Pending' WHERE id=?", (lid,)); conn.commit(); conn.close()
s, j, _ = call("POST", "/api/leaves/withdraw", {"id": lid}, T); check("withdraw pending leave", s == 200 and j["code"] == "leave_withdrawn", j)
s, j, _ = call("POST", "/api/leaves/credit", {"work_date": "2026-11-14", "note": "worked", "full_day": False}, T); check("credit needs full-day tick", s == 400 and j["code"] == "credit_full_day")

# ---- travel
png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
body, ct = multipart({"claim_date": TODAY, "travel_mode": "Own vehicle", "kms": "10", "client_name": "API Client", "venue_particular": "Visit"}, "receipt", "r.png", png)
s, j, _ = call("POST", "/api/travel", raw=body, ctype=ct, token=T); check("travel claim with receipt", s == 200 and j["code"] == "claim_submitted", j)
body, ct = multipart({"claim_date": TODAY, "kms": "10"}, "receipt", "bad.png", b"MZ not an image")
s, j, _ = call("POST", "/api/travel", raw=body, ctype=ct, token=T); check("bad receipt file rejected", s == 400 and j["code"] == "doc_bad_file", j)
body, ct = multipart({"claim_date": TODAY, "kms": "inf"})
s, j, _ = call("POST", "/api/travel", raw=body, ctype=ct, token=T); check("infinite kms rejected", s == 400)
body, ct = multipart({"claim_date": (date.today() + timedelta(days=2)).isoformat(), "kms": "5"})
s, j, _ = call("POST", "/api/travel", raw=body, ctype=ct, token=T); check("future claim rejected", s == 400 and j["code"] == "claim_future")
s, j, _ = call("GET", "/api/travel", token=T); cl = [x for x in j["data"]["claims"] if x["client_name"] == "API Client"]
check("claim listed, rate applied, stored filename hidden", len(cl) == 1 and cl[0]["has_receipt"] and "receipt_file" not in cl[0] and cl[0]["total_amount"] > 0, j)
s, j, _ = call("POST", "/api/travel/withdraw", {"id": cl[0]["id"]}, TB); check("can't withdraw someone else's claim", s == 403 and j["code"] == "not_allowed")
s, j, _ = call("POST", "/api/travel/withdraw", {"id": cl[0]["id"]}, T); check("withdraw claim", s == 200 and j["code"] == "claim_withdrawn", j)

# ---- loans
s, j, _ = call("GET", "/api/loans", token=T); before = j["data"]; check("loan limits", s == 200 and before["max_instalments"] == 20 and before["available"] >= 0, j)
s, j, _ = call("POST", "/api/loans", {"loan_type": "Loan", "amount": 1000, "instalments": 99, "purpose": "x"}, T); check("too many instalments rejected", s == 400 and j["code"] == "loan_instalments")
s, j, _ = call("POST", "/api/loans", {"loan_type": "Loan", "amount": "inf", "instalments": 2, "purpose": "x"}, T); check("infinite amount rejected", s == 400)
s, j, _ = call("POST", "/api/loans", {"loan_type": "Loan", "amount": 1000, "instalments": 2, "purpose": "API loan"}, T); check("apply for loan", s == 200 and j["code"] == "loan_applied", j)
s, j, _ = call("GET", "/api/loans", token=T); la = [x for x in j["data"]["applications"] if x["purpose"] == "API loan"]
check("pending application counts in outstanding", len(la) == 1 and j["data"]["outstanding"] == before["outstanding"] + 1000, j)
s, j, _ = call("POST", "/api/loans/withdraw", {"id": la[0]["id"]}, TB); check("can't withdraw someone else's loan", s == 400 and j["code"] == "loan_wrong_stage")
s, j, _ = call("POST", "/api/loans/withdraw", {"id": la[0]["id"]}, T); check("withdraw loan", s == 200 and j["code"] == "loan_withdrawn", j)

# ---- payslip
conn = app.get_conn(); app.calculate_payroll("2026-11-01")
conn.execute("UPDATE payroll_runs SET status='Draft' WHERE payroll_month='2026-11-01'"); conn.commit()
s, j, _ = call("GET", "/api/payslip?ym=2026-11", token=T); check("draft payslip hidden", s == 404 and j["code"] == "payslip_not_ready", j)
conn.execute("UPDATE payroll_runs SET status='Approved' WHERE payroll_month='2026-11-01'"); conn.commit()
s, j, _ = call("GET", "/api/payslip?ym=2026-11", token=T); check("approved payslip is own", s == 200 and j["data"]["payslip"]["emp_id"] == A and "held_balance" in j["data"], j)

# ---- payroll no longer waits for an uploaded report
conn.execute("UPDATE payroll_runs SET status='Draft' WHERE payroll_month='2026-11-01'"); conn.execute("DELETE FROM attendance_uploads"); conn.commit(); conn.close()
loc = web_post(PR, "/prepare_month?ym=2026-11", {}); check("month can be prepared without a report", "month_prepared" in loc, loc)

# ---- web UI
s, j, h = call("GET", "/"); check("web home still redirects to login", s == 302 and h["Location"] == "/login")
s, j, h = call("GET", "/login"); check("web login page loads", s == 200)
cookie = web_login("t_other", "Password#2")
s, html = web_get(cookie, "/?tab=leaves"); check("web leaves page renders", s == 200 and "<html" in html)
s, html = web_get(AP, "/?tab=payroll&ym=2026-11"); check("payroll page renders without the upload warning", s == 200 and "Attendance not uploaded" not in html)
c = sqlite3.connect(db); row = c.execute("SELECT client_type FROM sessions WHERE token=?", (cookie.split("=", 1)[1],)).fetchone(); c.close()
check("web session is type web", row[0] == "web")

# ---- logout
s, j, _ = call("POST", "/api/logout", {}, T); check("logout", s == 200 and j["code"] == "logged_out")
s, j, _ = call("GET", "/api/dashboard", token=T); check("token dead after logout", s == 401)

srv.shutdown(); srv.server_close()
print(f"\n{sum(results)} of {len(results)} checks passed")
shutil.rmtree(work, ignore_errors=True)
sys.exit(0 if all(results) else 1)
