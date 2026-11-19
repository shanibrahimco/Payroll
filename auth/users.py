"""User accounts, passwords and the login attempt limit."""

from datetime import date
from datetime import datetime
import hashlib
import hmac
import secrets

from auth.roles import ROLE_PREPARER
from database import get_conn

PBKDF2_ROUNDS = 200_000

def hash_password(password, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return salt.hex(), digest.hex()

def user_count():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return n

def create_user(username, password, role=ROLE_PREPARER, emp_id=None, must_change=False):
    salt_hex, hash_hex = hash_password(password)
    conn = get_conn()
    conn.execute("INSERT INTO users (username, password_salt, password_hash, created_at, role, emp_id, must_change_pw) VALUES (?,?,?,?,?,?,?)",
                 (username, salt_hex, hash_hex, datetime.now().isoformat(timespec="seconds"), role, emp_id, 1 if must_change else 0))
    conn.commit()
    conn.close()

def get_user(username):
    conn = get_conn()
    row = conn.execute("SELECT username, role, emp_id, must_change_pw FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return row

def normalise_username(username):
    """Staff user IDs are their CNIC without dashes; accept it typed with dashes too."""
    digits = username.replace("-", "").replace(" ", "")
    return digits if len(digits) == 13 and digits.isdigit() else username

def set_password(username, password):
    salt_hex, hash_hex = hash_password(password)
    conn = get_conn()
    conn.execute("UPDATE users SET password_salt = ?, password_hash = ?, must_change_pw = 0 WHERE username = ?", (salt_hex, hash_hex, username))
    conn.commit()
    conn.close()

def delete_user(username):
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
    conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    conn.close()

def staff_access_ended(row):
    """A leaver keeps access through their last working day; with no leaving date on record, access ends once marked Left."""
    if row["leaving_date"]:
        return row["leaving_date"] < date.today().isoformat()
    return row["status"] == "Left"

def access_removed(conn, username):
    row = conn.execute("""SELECT s.status, s.leaving_date FROM users u JOIN staff s ON s.emp_id = u.emp_id
                          WHERE u.username = ?""", (username,)).fetchone()
    return row is not None and staff_access_ended(row)

def verify_login(username, password):
    conn = get_conn()
    row = conn.execute("SELECT password_salt, password_hash FROM users WHERE username = ?", (username,)).fetchone()
    removed = access_removed(conn, username)
    conn.close()
    if row is None:
        hash_password(password)  # same work for unknown users, so timing doesn't reveal valid usernames
        return False
    _, hash_hex = hash_password(password, row["password_salt"])
    return hmac.compare_digest(hash_hex, row["password_hash"]) and not removed

LOGIN_WINDOW_SECONDS = 15 * 60

LOGIN_MAX_USER_FAILURES = 5  # wrong passwords for one username within the window

LOGIN_MAX_IP_FAILURES = 20  # wrong passwords from one outside address within the window, whichever usernames were tried

LOGIN_FAILURES = {}  # key -> times of recent wrong passwords

def try_login(username, password, ip):
    """'ok', 'invalid', or 'locked' after too many wrong passwords (the password isn't even checked then). Connections arriving
    through a local tunnel all share one address, so only outside addresses are limited by address."""
    now = datetime.now().timestamp()
    for key in list(LOGIN_FAILURES):
        LOGIN_FAILURES[key] = [t for t in LOGIN_FAILURES[key] if now - t < LOGIN_WINDOW_SECONDS]
        if not LOGIN_FAILURES[key]:
            del LOGIN_FAILURES[key]
    user_key = f"user:{username.lower()}"
    limits = [(user_key, LOGIN_MAX_USER_FAILURES)]
    if ip not in ("127.0.0.1", "::1"):
        limits.append((f"ip:{ip}", LOGIN_MAX_IP_FAILURES))
    if any(len(LOGIN_FAILURES.get(key, [])) >= limit for key, limit in limits):
        return "locked"
    if verify_login(username, password):
        LOGIN_FAILURES.pop(user_key, None)
        return "ok"
    for key, _ in limits:
        LOGIN_FAILURES.setdefault(key, []).append(now)
    return "invalid"
