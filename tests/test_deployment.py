"""Checks the settings the server deployment depends on, by starting the real app in temporary folders. Live data is never touched.

Run from the project folder:  python tests/test_deployment.py
"""
import json, os, shutil, socket, sqlite3, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request, zipfile
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
work = tempfile.mkdtemp(prefix="payroll_deploy_test_")
results = []

def check(name, condition, detail=""):
    results.append(bool(condition))
    print("PASS" if condition else "FAIL", name, detail if not condition else "")

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

opener = urllib.request.build_opener(NoRedirect)

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def http(port, path, data=None, headers=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {})
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    return response.status if hasattr(response, "status") else response.code, response.headers, response.read()

def start(data_dir, **env):
    port = free_port()
    settings = {**os.environ, "PAYROLL_DATA_DIR": data_dir, "PAYROLL_PORT": str(port), "PYTHONDONTWRITEBYTECODE": "1", **env}
    process = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=settings, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        try:
            if http(port, "/health")[0] == 200:
                return process, port
        except OSError:
            time.sleep(0.2)
    process.kill()
    raise RuntimeError("the app did not start")

def form(**fields):
    return urllib.parse.urlencode(fields).encode()

try:
    # ---- on a server, behind the HTTPS proxy
    data = os.path.join(work, "server_data")
    os.makedirs(data)
    process, port = start(data, PAYROLL_BEHIND_HTTPS_PROXY="1", PAYROLL_PUBLIC_URL="https://payroll.example.test/")
    status, headers, body = http(port, "/health")
    check("health address answers", status == 200 and json.loads(body) == {"status": "ok"}, body)
    check("health address shows no business data", len(body) < 40)
    check("database is created in the data folder", os.path.exists(os.path.join(data, "sic_payroll.db")))
    status, headers, _ = http(port, "/setup", form(username="deploy_admin", password="Deploy#Admin1", confirm="Deploy#Admin1"))
    cookie = headers.get("Set-Cookie", "")
    check("login cookie is HTTPS-only on the server", status == 302 and "Secure" in cookie and "HttpOnly" in cookie, cookie)
    session = cookie.split(";")[0]
    status, _, body = http(port, "/?tab=payroll", headers={"Cookie": session})
    check("app opens on the current month", status == 200 and date.today().strftime("%B %Y").encode() in body)
    c = sqlite3.connect(os.path.join(data, "sic_payroll.db"))
    check("login emails use the live address", c.execute("SELECT value FROM settings WHERE key='app_url'").fetchone()[0] == "https://payroll.example.test")
    c.close()
    for i in range(20):
        http(port, "/login", form(username=f"guess{i}", password="wrong-password"), {"X-Forwarded-For": "203.0.113.50"})
    status, _, _ = http(port, "/login", form(username="deploy_admin", password="Deploy#Admin1"), {"X-Forwarded-For": "203.0.113.50"})
    check("20 wrong passwords block that visitor's address", status == 429, status)
    status, _, _ = http(port, "/login", form(username="deploy_admin", password="Deploy#Admin1"), {"X-Forwarded-For": "198.51.100.7"})
    check("other visitors can still sign in", status == 302, status)
    status, _, _ = http(port, "/login", form(username="deploy_admin", password="Deploy#Admin1"), {"X-Forwarded-For": "198.51.100.7, 203.0.113.50"})
    check("a faked address can't get around the block", status == 429, status)
    process.kill(); process.wait()

    # ---- on this computer: no server settings
    data2 = os.path.join(work, "local_data")
    os.makedirs(data2)
    process, port = start(data2)
    status, headers, _ = http(port, "/setup", form(username="local_admin", password="Local#Admin1", confirm="Local#Admin1"))
    check("login cookie still works without HTTPS on this computer", status == 302 and "Secure" not in headers.get("Set-Cookie", ""))
    for i in range(20):
        http(port, "/login", form(username=f"guess{i}", password="wrong-password"), {"X-Forwarded-For": "203.0.113.50"})
    status, _, _ = http(port, "/login", form(username="local_admin", password="Local#Admin1"), {"X-Forwarded-For": "203.0.113.50"})
    check("forwarded addresses are ignored without the proxy", status == 302, status)
    process.kill(); process.wait()

    # ---- clean shutdown
    data3 = os.path.join(work, "stop_data")
    os.makedirs(data3)
    port = free_port()
    script = os.path.join(work, "stop_check.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(f"""import signal, sys, threading, time, urllib.request
sys.path.insert(0, {ROOT!r})
import config
from database import init_db
from payroll.seed import seed_from_excel
from web.handler import run_server
init_db(); seed_from_excel()
def later():
    for _ in range(100):
        try:
            urllib.request.urlopen('http://127.0.0.1:{port}/health', timeout=2); break
        except OSError:
            time.sleep(0.1)
    signal.raise_signal(signal.SIGTERM)
threading.Thread(target=later, daemon=True).start()
run_server('127.0.0.1', {port})
""")
    done = subprocess.run([sys.executable, script], cwd=work, env={**os.environ, "PAYROLL_DATA_DIR": data3, "PYTHONDONTWRITEBYTECODE": "1"},
                          capture_output=True, text=True, timeout=60)
    check("server stops cleanly when Docker asks it to", done.returncode == 0 and "stopped cleanly" in done.stdout, done.stdout + done.stderr[-300:])

    # ---- daily backup
    os.makedirs(os.path.join(data, "uploads", "receipts"))
    with open(os.path.join(data, "uploads", "receipts", "r.png"), "wb") as f:
        f.write(b"\x89PNG test")
    backups = os.path.join(work, "backups")
    os.makedirs(backups)
    old = os.path.join(backups, "payroll_backup_2020-01-01_0200.zip")
    with zipfile.ZipFile(old, "w") as z:
        z.writestr("x", "old")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    env = {**os.environ, "PAYROLL_DATA_DIR": data, "PAYROLL_BACKUP_DIR": backups, "BACKUP_KEEP_DAYS": "14", "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, os.path.join(ROOT, "deploy", "backup.py"), "--once"], env=env, capture_output=True, text=True, timeout=60)
    made = [f for f in os.listdir(backups) if f.endswith(".zip") and "2020" not in f]
    check("backup is created", done.returncode == 0 and len(made) == 1, done.stdout + done.stderr)
    check("backups past the keep period are removed", not os.path.exists(old))
    with zipfile.ZipFile(os.path.join(backups, made[0])) as z:
        names = z.namelist()
        z.extract("sic_payroll.db", os.path.join(work, "restored"))
    check("backup holds the database and uploads", "sic_payroll.db" in names and "uploads/receipts/r.png" in names, names)
    c = sqlite3.connect(os.path.join(work, "restored", "sic_payroll.db"))
    check("backed-up database can be restored", c.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1)
    c.close()

    # ---- nothing private goes into the image or to GitHub
    allowed = [l.strip() for l in open(os.path.join(ROOT, ".dockerignore"), encoding="utf-8") if l.strip() and not l.startswith("#")]
    check("image starts by excluding everything", allowed[0] == "*")
    check("image can't include data files", not any(w in l.lower() for l in allowed for w in (".db", ".env", ".zip", "uploads", "exports", "local_settings", "tests")), allowed)
finally:
    shutil.rmtree(work, ignore_errors=True)

print(f"\n{sum(results)} of {len(results)} checks passed")
sys.exit(0 if all(results) else 1)
