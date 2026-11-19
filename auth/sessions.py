"""Login sessions and delegated portals."""

from datetime import datetime
from datetime import timedelta
import secrets

from auth.roles import ROLE_REVIEWER
from auth.users import access_removed
from database import get_conn

SESSION_COOKIE = "sic_session"

SESSION_HOURS = 8

MOBILE_SESSION_DAYS = 30

def create_session(username, client_type="web"):
    token = secrets.token_urlsafe(32)
    lifetime = timedelta(days=MOBILE_SESSION_DAYS) if client_type == "mobile" else timedelta(hours=SESSION_HOURS)
    expires = (datetime.now() + lifetime).isoformat(timespec="seconds")
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.now().isoformat(timespec="seconds"),))
    conn.execute("INSERT INTO sessions (token, username, expires_at, client_type) VALUES (?,?,?,?)", (token, username, expires, client_type))
    conn.commit()
    conn.close()
    return token

def get_session_user(token):
    if not token:
        return None
    conn = get_conn()
    row = conn.execute("SELECT username, expires_at FROM sessions WHERE token = ?", (token,)).fetchone()
    removed = row is not None and access_removed(conn, row["username"])  # a leaver already logged in is signed out too
    conn.close()
    if row is None or removed or row["expires_at"] < datetime.now().isoformat(timespec="seconds"):
        return None
    return row["username"]

def set_session_acting(token, emp_id):
    conn = get_conn()
    conn.execute("UPDATE sessions SET acting_for = ? WHERE token = ?", (emp_id, token))
    conn.commit()
    conn.close()

def delegated_portals(conn, username):
    """Staff whose portal this user operates: [(emp_id, employee_name)]."""
    return conn.execute("""SELECT d.emp_id, s.employee_name FROM portal_delegates d JOIN staff s ON s.emp_id = d.emp_id
                           JOIN users u ON u.emp_id = d.emp_id WHERE d.delegate_username = ? ORDER BY d.emp_id""", (username,)).fetchall()

def approver_only_emps(conn):
    """Staff whose leave, travel, loans and timesheets the reviewer must not decide, so they go straight to the approver:
    the reviewer himself, and staff whose portal the reviewer operates for them."""
    return {r[0] for r in conn.execute("""
        SELECT emp_id FROM users WHERE role = ? AND emp_id IS NOT NULL
        UNION SELECT d.emp_id FROM portal_delegates d JOIN users u ON u.username = d.delegate_username WHERE u.role = ?
    """, (ROLE_REVIEWER, ROLE_REVIEWER))}

def delete_session(token):
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()
