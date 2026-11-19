"""User accounts, passwords and portal operators."""

from auth.roles import ROLES, ROLE_STAFF
from auth.users import create_user, delete_user, set_password, verify_login
from common.constants import MIN_PASSWORD_LEN
from common.errors import ChangeError
from common.settings import log_decision

def handle(self, conn, path, data, ym, pm_str, user, role, own_emp, cfg, cutoff, km_rate, now, enters_for_others):
    if path in ("/set_delegate", "/remove_delegate"):
        emp_id = data.get("emp_id", "").strip().upper()
        staff = conn.execute("""SELECT s.employee_name FROM staff s JOIN users u ON u.emp_id = s.emp_id
                                    WHERE s.emp_id = ? AND u.role = ?""", (emp_id, ROLE_STAFF)).fetchone()
        if staff is None:
            raise ChangeError("delegate_invalid")
        if path == "/remove_delegate":
            conn.execute("DELETE FROM portal_delegates WHERE emp_id = ?", (emp_id,))
            conn.execute("UPDATE sessions SET acting_for = NULL WHERE acting_for = ?", (emp_id,))
            log_decision(conn, "portal_delegate", emp_id, None, {"delegate": None}, f"{emp_id} {staff['employee_name']}: portal operator removed", user)
            return "delegate_removed"
        delegate = data.get("delegate_username", "")
        d = conn.execute("SELECT username, role, emp_id FROM users WHERE username = ?", (delegate,)).fetchone()
        if d is None or d["role"] == ROLE_STAFF or d["emp_id"] == emp_id:
            raise ChangeError("delegate_invalid")
        conn.execute("INSERT OR REPLACE INTO portal_delegates (emp_id, delegate_username, granted_by, granted_at) VALUES (?,?,?,?)",
                     (emp_id, delegate, user, now))
        log_decision(conn, "portal_delegate", emp_id, None, {"delegate": delegate},
                     f"{emp_id} {staff['employee_name']}: portal operated by {delegate} ({d['role']})", user)
        return "delegate_saved"

    if path == "/change_password":
        if (not verify_login(user, data.get("current", "")) or len(data.get("new", "")) < MIN_PASSWORD_LEN
                or data.get("new") != data.get("confirm") or data.get("new") == data.get("current")):
            raise ChangeError("pw_invalid")
        set_password(user, data["new"])
        return "pw_changed"

    if path == "/add_user":
        username = data.get("username", "").strip()
        new_role = data.get("role")
        emp_id = data.get("emp_id", "").strip().upper() or None
        if not username or len(data.get("password", "")) < MIN_PASSWORD_LEN or new_role not in ROLES:
            raise ChangeError("user_invalid")
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise ChangeError("user_exists")
        if (new_role == ROLE_STAFF and not emp_id) or (emp_id and not conn.execute("SELECT 1 FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()):
            raise ChangeError("need_emp")
        conn.commit()
        create_user(username, data["password"], new_role, emp_id, must_change=True)
        return "user_added"

    if path == "/delete_user":
        if data.get("username") == user:
            raise ChangeError("cannot_delete_self")
        conn.commit()
        delete_user(data.get("username", ""))
        return "user_deleted"
