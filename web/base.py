"""Basic request and response helpers."""

from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from auth.roles import ROLE_STAFF
from auth.sessions import SESSION_COOKIE, get_session_user, set_session_acting
from auth.users import get_user
from common.constants import MAX_FORM_BYTES
from database import get_conn
from web.forms import parse_multipart

class HandlerBase:
    def log_message(self, format, *args):
        pass

    def redirect(self, location, cookie=None):
        self.send_response(302)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def send_html(self, html, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def session_token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else None

    def current_user(self):
        return get_session_user(self.bearer_token() or self.session_token())

    def bearer_token(self):
        header = self.headers.get("Authorization", "")
        return header[7:].strip() or None if header[:7].lower() == "bearer " else None

    def read_form(self):
        """Form fields as a dict; uploaded files (multipart forms only) go to self.files."""
        self.files = {}
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_FORM_BYTES:
            while length > 0:  # drain an oversized upload so the browser gets the reply
                length -= len(self.rfile.read(min(length, 1 << 20)) or b"x" * length)
            self.files = {"scan": ("too_large", "", b"\0")}  # rejected by the file-type check
            return {}
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            fields, self.files = parse_multipart(content_type, body)
            return fields
        return {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}

    def effective_account(self, real_user):
        """(account, user label, staff name being operated or None). While a user operates a delegated portal they act
        with that staff member's account; everything they do is recorded as '<user> for <staff name>'."""
        token = self.session_token()
        conn = get_conn()
        row = conn.execute("SELECT acting_for FROM sessions WHERE token = ?", (token,)).fetchone()
        acting = conn.execute("""SELECT u.username, u.role, u.emp_id, u.must_change_pw, s.employee_name FROM portal_delegates d
                                 JOIN users u ON u.emp_id = d.emp_id JOIN staff s ON s.emp_id = d.emp_id
                                 WHERE d.emp_id = ? AND d.delegate_username = ? AND u.role = ?""",
                              (row["acting_for"], real_user, ROLE_STAFF)).fetchone() if row and row["acting_for"] else None
        conn.close()
        if acting:
            return acting, f"{real_user} for {acting['employee_name']}", acting["employee_name"]
        if row and row["acting_for"]:
            set_session_acting(token, None)  # delegation was removed
        return get_user(real_user), real_user, None
