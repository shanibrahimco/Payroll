"""Checks lump-sum loans and loan repayments on a temporary company with made-up staff. Live data is never touched.

Run from the project folder:  python tests/test_loans.py
"""
import json, os, re, shutil, sys, tempfile, threading, urllib.error, urllib.parse, urllib.request
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.dont_write_bytecode = True
work = tempfile.mkdtemp(prefix="payroll_loans_test_")
import config
config.COMPANIES["sic"].update(db=os.path.join(work, "t.db"), exports=os.path.join(work, "exports"), uploads=os.path.join(work, "uploads"), excel=None)
config.use_company("sic")
assert config.DB_PATH.startswith(work)
from auth.users import create_user
from common.dates import add_months, first_of_month, fmt_date
from database import get_conn, init_db
from payroll.calculator import calculate_payroll
from payroll.seed import seed_from_excel
from web.handler import PayrollHandler, PayrollServer

init_db(); seed_from_excel()
c = get_conn()
c.execute("INSERT OR REPLACE INTO banks (bank_name, iban_code, ibft_bank_code, is_meezan) VALUES ('Test Bank','TEST','TESTPKKA',0)")
for emp, name, salary in (("T01", "Test Lump Sum", 100000), ("T02", "Test Instalments", 60000), ("T03", "Test Reviewer", 80000)):
    c.execute("""INSERT INTO staff (emp_id, employee_name, staff_type, date_of_joining, monthly_salary, status, bank_name, bank_account_iban)
                 VALUES (?,?,'Permanent','2024-01-01',?,'Active','Test Bank','PK00TEST0000000001')""", (emp, name, salary))
c.commit(); c.close()
for u, role, emp in (("t_appr", "Approver", None), ("t_rev", "Reviewer", "T03"), ("t_prep", "Preparer", None), ("t_s1", "Staff", "T01"), ("t_s2", "Staff", "T02")):
    create_user(u, "Password#1x", role, emp)
srv = PayrollServer(("127.0.0.1", 0), PayrollHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
results = []
def check(name, cond, detail=""):
    results.append(bool(cond)); print("PASS" if cond else "FAIL", name, detail if not cond else "")
class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
op = urllib.request.build_opener(NR)
def http(method, path, data=None, headers=None):
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    try: resp = op.open(r, timeout=60)
    except urllib.error.HTTPError as e: resp = e
    return resp.status if hasattr(resp, "status") else resp.code, resp.headers, resp.read()
COOKIES = {}
def cookie(u):
    if u not in COOKIES:
        s, h, _ = http("POST", "/login", urllib.parse.urlencode({"username": u, "password": "Password#1x"}).encode())
        COOKIES[u] = h["Set-Cookie"].split(";")[0]
    return COOKIES[u]
def act(u, path, **fields):
    s, h, _ = http("POST", path + "?ym=" + date.today().strftime("%Y-%m"), urllib.parse.urlencode(fields).encode(), {"Cookie": cookie(u)})
    m = re.search(r"msg=(\w+)", h.get("Location", "")); return m.group(1) if m else f"HTTP{s}"
def one(sql, *a):
    c = get_conn(); r = c.execute(sql, a).fetchone(); c.close(); return r
today = date.today()
this_month = first_of_month(today)
due = add_months(today, 3)

try:
    # ---- applying for a lump-sum loan
    eq = lambda name, got, want: check(name, got == want, f"got {got!r}, expected {want!r}")
    eq("lump sum needs a committed date", act("t_s1", "/apply_loan", loan_type="Loan", amount="150000", repayment="Lump sum", purpose="House"), "loan_lump_date")
    eq("lump sum date can't be today or earlier", act("t_s1", "/apply_loan", loan_type="Loan", amount="150000", repayment="Lump sum", lump_sum_date=today.isoformat(), purpose="House"), "loan_lump_date")
    eq("lump sum date can't be beyond the longest loan period", act("t_s1", "/apply_loan", loan_type="Loan", amount="150000", repayment="Lump sum",
                                                                    lump_sum_date=add_months(today, 21).isoformat(), purpose="House"), "loan_lump_date")
    eq("lump-sum loan applied", act("t_s1", "/apply_loan", loan_type="Loan", amount="150000", repayment="Lump sum", lump_sum_date=due.isoformat(), purpose="House"), "loan_applied")
    app = one("SELECT * FROM loan_applications ORDER BY id DESC LIMIT 1")
    check("application records lump sum and date", app["repayment_method"] == "Lump sum" and app["lump_sum_date"] == due.isoformat(), dict(app))
    s, h, b = http("GET", "/?tab=loans", headers={"Cookie": cookie("t_s1")})
    check("staff see the lump-sum option and their application", s == 200 and b"Lump sum by a date" in b and f"Lump sum by {due.isoformat()}".encode() in b)
    eq("reviewer recommends", act("t_rev", "/review_loan", id=app["id"], decision="Recommended"), "loan_recommended")
    eq("approver approves", act("t_appr", "/decide_loan", id=app["id"], decision="Approved"), "loan_approved")
    ln = one("SELECT * FROM advances_loans WHERE loan_ref = ?", f"LA{app['id']:04d}")
    check("approved loan is lump sum with no instalment", ln["repayment_method"] == "Lump sum" and ln["lump_sum_due"] == due.isoformat() and ln["monthly_instalment"] == 0, dict(ln))
    calculate_payroll(fmt_date(add_months(this_month, 1)))
    eq("nothing is deducted from salary for a lump-sum loan", one("SELECT loan_advance_recovery FROM payroll_lines WHERE emp_id='T01' AND payroll_month=?", fmt_date(add_months(this_month, 1)))[0], 0)
    after_due = fmt_date(add_months(first_of_month(due), 1))
    calculate_payroll(after_due)
    note = one("SELECT validation_checks FROM payroll_lines WHERE emp_id='T01' AND payroll_month=?", after_due)[0]
    check("payroll flags a lump sum not repaid by its date", "lump sum of Rs. 150,000" in note and due.isoformat() in note, note)

    # ---- recording repayments
    eq("staff can't record repayments", act("t_s1", "/record_loan_repayment", loan_ref=ln["loan_ref"], amount="1000", paid_on=today.isoformat()), "not_allowed")
    eq("preparer can't record repayments", act("t_prep", "/record_loan_repayment", loan_ref=ln["loan_ref"], amount="1000", paid_on=today.isoformat()), "not_allowed")
    eq("future repayment date refused", act("t_appr", "/record_loan_repayment", loan_ref=ln["loan_ref"], amount="1000", paid_on=add_months(today, 1).isoformat()), "repayment_invalid")
    eq("repayment above the balance refused", act("t_appr", "/record_loan_repayment", loan_ref=ln["loan_ref"], amount="150001", paid_on=today.isoformat()), "repayment_too_much")
    eq("infinite repayment refused", act("t_appr", "/record_loan_repayment", loan_ref=ln["loan_ref"], amount="inf", paid_on=today.isoformat()), "repayment_invalid")
    eq("first part repaid", act("t_appr", "/record_loan_repayment", loan_ref=ln["loan_ref"], amount="50000", paid_on=today.isoformat(), note="Cash"), "repayment_saved")
    calculate_payroll(after_due)
    note = one("SELECT validation_checks FROM payroll_lines WHERE emp_id='T01' AND payroll_month=?", after_due)[0]
    check("overdue amount drops after a part payment", "lump sum of Rs. 100,000" in note, note)
    eq("rest repaid", act("t_appr", "/record_loan_repayment", loan_ref=ln["loan_ref"], amount="100000", paid_on=today.isoformat()), "repayment_saved")
    calculate_payroll(after_due)
    check("fully repaid loan is no longer flagged", "lump sum" not in (one("SELECT validation_checks FROM payroll_lines WHERE emp_id='T01' AND payroll_month=?", after_due)[0] or ""))
    eq("nothing more can be repaid", act("t_appr", "/record_loan_repayment", loan_ref=ln["loan_ref"], amount="1", paid_on=today.isoformat()), "repayment_too_much")
    log = one("SELECT summary FROM change_requests ORDER BY id DESC LIMIT 1")[0]
    check("approval log records the repayment", "fully repaid" in log, log)

    # ---- repayment on an instalment loan reduces salary deductions
    c = get_conn()
    c.execute("""INSERT INTO advances_loans (loan_ref, emp_id, loan_type, date_given, amount, monthly_instalment, first_recovery_month, repayment_method)
                 VALUES ('TL1','T02','Loan',?,100000,20000,?,'Instalments')""", (fmt_date(add_months(this_month, -2)), fmt_date(add_months(this_month, -1))))
    c.commit(); c.close()
    calculate_payroll(fmt_date(this_month))
    eq("instalment deducted before any repayment", one("SELECT loan_advance_recovery FROM payroll_lines WHERE emp_id='T02' AND payroll_month=?", fmt_date(this_month))[0], 20000)
    eq("part repayment on an instalment loan", act("t_appr", "/record_loan_repayment", loan_ref="TL1", amount="70000", paid_on=today.isoformat()), "repayment_saved")
    calculate_payroll(fmt_date(this_month))
    eq("only what is left is deducted this month", one("SELECT loan_advance_recovery FROM payroll_lines WHERE emp_id='T02' AND payroll_month=?", fmt_date(this_month))[0], 10000)
    calculate_payroll(fmt_date(add_months(this_month, 1)))
    eq("nothing deducted once the loan is cleared", one("SELECT loan_advance_recovery FROM payroll_lines WHERE emp_id='T02' AND payroll_month=?", fmt_date(add_months(this_month, 1)))[0], 0)

    # ---- pages and the mobile app
    s, h, b = http("GET", "/?tab=loans", headers={"Cookie": cookie("t_appr")})
    check("approver sees repayments and the balance", s == 200 and b"Loan Repayments Made Directly" in b and b"Record Repayment" in b and b"Repaid" in b)
    s, h, b = http("GET", "/?tab=loans", headers={"Cookie": cookie("t_s2")})
    check("staff see their own repayments only", s == 200 and b"70,000" in b and b"Test Lump Sum" not in b)
    s, h, b = http("POST", "/api/login", json.dumps({"username": "t_s2", "password": "Password#1x"}).encode(), {"Content-Type": "application/json"})
    tok = json.loads(b)["token"]
    s, h, b = http("GET", "/api/loans", headers={"Authorization": "Bearer " + tok}); d = json.loads(b)["data"]
    loan = d["active_loans"][0] if d["active_loans"] else None
    check("mobile app shows repayments and the balance", loan and loan["repaid_directly"] == 70000 and loan["balance"] == 10000 and len(d["repayments"]) == 1
          and "Lump sum" in d["repayment_methods"], d)
    s, h, b = http("POST", "/api/loans", json.dumps({"loan_type": "Loan", "amount": 20000, "repayment": "Lump sum", "lump_sum_date": due.isoformat(), "purpose": "Mobile"}).encode(),
                   {"Content-Type": "application/json", "Authorization": "Bearer " + tok})
    check("mobile app can apply for a lump-sum loan", s == 200 and one("SELECT repayment_method FROM loan_applications ORDER BY id DESC LIMIT 1")[0] == "Lump sum", b[:200])
finally:
    srv.shutdown(); srv.server_close(); shutil.rmtree(work, ignore_errors=True)
print(f"\n{sum(results)} of {len(results)} checks passed")
sys.exit(0 if all(results) else 1)
