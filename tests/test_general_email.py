"""Checks the Email All Staff option on a temporary company, with a local test mail server. No real email is sent.

Run from the project folder:  python tests/test_general_email.py
"""
import os, re, shutil, socket, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.dont_write_bytecode = True
work = tempfile.mkdtemp(prefix="payroll_email_test_")
import config
config.COMPANIES["sic"].update(db=os.path.join(work, "t.db"), exports=os.path.join(work, "exports"), uploads=os.path.join(work, "uploads"), excel=None)
config.use_company("sic")
assert config.DB_PATH.startswith(work)
from auth.users import create_user
from database import get_conn, init_db
from payroll.seed import seed_from_excel
from web.handler import PayrollHandler, PayrollServer

MAIL, PORTS = [], []
def mail_server():  # accepts sign-in and keeps every message instead of sending it
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", 0)); s.listen(5); PORTS.append(s.getsockname()[1])
    while True:
        conn, _ = s.accept(); f = conn.makefile("rb"); conn.sendall(b"220 test\r\n"); data, buf, rcpt = False, b"", []
        try:
            for raw in f:
                if data:
                    if raw == b".\r\n":
                        MAIL.append({"to": rcpt, "body": buf.decode("utf-8", "replace")}); data, buf, rcpt = False, b"", []; conn.sendall(b"250 ok\r\n")
                    else:
                        buf += raw
                    continue
                cmd = raw[:4].upper()
                if cmd == b"EHLO": conn.sendall(b"250-test\r\n250 AUTH PLAIN LOGIN\r\n")
                elif cmd == b"AUTH": conn.sendall(b"235 ok\r\n")
                elif cmd == b"DATA": data = True; conn.sendall(b"354 go\r\n")
                elif cmd == b"QUIT": conn.sendall(b"221 bye\r\n"); break
                else:
                    if cmd == b"RCPT": rcpt.append(raw.decode().strip())
                    conn.sendall(b"250 ok\r\n")
        except OSError:
            pass
        conn.close()
threading.Thread(target=mail_server, daemon=True).start()
while not PORTS: time.sleep(0.01)

init_db(); seed_from_excel()
c = get_conn()
staff = (("T01", "Test Active One", "Permanent", "Active", "one@example.com"), ("T02", "Test Active Two", "ICAP Article", "Active", "two@example.com"),
         ("T03", "Test No Email", "Permanent", "Active", None), ("T04", "Test Left", "Permanent", "Left", "left@example.com"),
         ("T05", "Test Eobi Only", "EOBI Only", "Active", "eobi@example.com"), ("T06", "Test Reviewer", "Permanent", "Active", "rev@example.com"))
for emp, name, stype, status, email in staff:
    c.execute("INSERT INTO staff (emp_id, employee_name, staff_type, date_of_joining, monthly_salary, status, email) VALUES (?,?,?,'2024-01-01',50000,?,?)",
              (emp, name, stype, status, email))
c.commit(); c.close()
for u, role, emp in (("t_appr", "Approver", None), ("t_rev", "Reviewer", "T06"), ("t_prep", "Preparer", None), ("t_staff", "Staff", "T01")):
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
def http(method, path, data=None, cookie=None):
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Cookie": cookie} if cookie else {})
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
    s, h, _ = http("POST", path, urllib.parse.urlencode(fields).encode(), cookie(u))
    m = re.search(r"msg=(\w+)", h.get("Location", "")); return m.group(1) if m else f"HTTP{s}"
def eq(name, got, want): check(name, got == want, f"got {got!r}, expected {want!r}")

try:
    eq("needs a sending account first", act("t_appr", "/email_all_staff", subject="Hello", message="Test"), "mail_no_account")
    for u, addr in (("t_appr", "approver@example.com"), ("t_rev", "rev@example.com")):
        eq(f"{u} sending account saved", act(u, "/save_mail_account", from_address=addr, from_name=f"Sender {u}", smtp_host="127.0.0.1",
                                              smtp_port=str(PORTS[0]), security="None", smtp_password="x"), "mail_account_saved")
    s, h, b = http("GET", "/?tab=email", cookie=cookie("t_appr"))
    check("approver sees Email All Staff with the right count", s == 200 and b"Email All Staff" in b and b"Send to All Staff (3)" in b, b[:0])
    check("page lists who has no email address", b"T03 Test No Email" in b)
    eq("subject is required", act("t_appr", "/email_all_staff", subject=" ", message="Body"), "general_email_invalid")
    eq("message is required", act("t_appr", "/email_all_staff", subject="Hello", message="  "), "general_email_invalid")
    eq("preparer can't send to all staff", act("t_prep", "/email_all_staff", subject="Hello", message="Body"), "not_allowed")
    eq("staff can't send to all staff", act("t_staff", "/email_all_staff", subject="Hello", message="Body"), "not_allowed")
    check("nothing was sent by the refused attempts", MAIL == [])
    eq("approver sends to all staff", act("t_appr", "/email_all_staff", subject="Office closed on Friday", message="The office will be closed.\nSee you Monday."), "general_email_sent")
    to = sorted(m["to"][0] for m in MAIL)
    check("goes only to active staff with an email address", len(MAIL) == 3 and all(any(a in t for t in to) for a in ("one@", "two@", "rev@")), to)
    check("left staff, EOBI-only and no-email people are skipped", not any(x in " ".join(to) for x in ("left@", "eobi@")))
    check("each person gets their own email", all(len(m["to"]) == 1 for m in MAIL))
    one = next(m["body"] for m in MAIL if "one@" in m["to"][0])
    check("email is personal and signed by the sender", "Dear Test Active One" in one and "See you Monday." in one and "Sender t_appr" in one and "Office closed on Friday" in one, one[-400:])
    c = get_conn(); log = c.execute("SELECT COUNT(*), SUM(status='Sent') FROM email_log WHERE purpose='General email'").fetchone(); c.close()
    eq("every email is in the Email Log", tuple(log), (3, 3))
    MAIL.clear()
    eq("reviewer sends to all staff from their own account", act("t_rev", "/email_all_staff", subject="<b>Note</b>", message="Hello <script>x</script>"), "general_email_sent")
    check("reviewer's email comes from the reviewer", len(MAIL) == 3 and all("Sender t_rev" in m["body"] for m in MAIL))
    s, h, b = http("GET", "/?tab=email", cookie=cookie("t_rev"))
    check("email page still opens and shows text safely", s == 200 and b"<script>x</script>" not in b)
finally:
    srv.shutdown(); srv.server_close(); shutil.rmtree(work, ignore_errors=True)
print(f"\n{sum(results)} of {len(results)} checks passed")
sys.exit(0 if all(results) else 1)
