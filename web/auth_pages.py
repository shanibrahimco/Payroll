"""Sign-in, first-time set-up and sign-out."""

from auth.roles import ROLE_APPROVER
from auth.sessions import SESSION_HOURS, create_session, delete_session
from auth.users import create_user, normalise_username, try_login, user_count
from common.constants import MIN_PASSWORD_LEN
from common.messages import FLASH_MESSAGES
from web.templates import render_auth_page

class AuthPagesMixin:
    def handle_auth_get(self, path):
        if user_count() == 0:
            if path != "/setup":
                return self.redirect("/setup")
            return self.send_html(render_auth_page(setup=True))
        if path == "/setup":
            return self.redirect("/login")
        if self.current_user():
            return self.redirect("/")
        return self.send_html(render_auth_page(setup=False))

    def handle_auth_post(self, path, data):
        username = normalise_username(data.get("username", "").strip())
        password = data.get("password", "")

        if path == "/logout":
            token = self.session_token()
            if token:
                delete_session(token)
            return self.redirect("/login", cookie=self.session_cookie("", 0))

        if path == "/setup":
            if user_count() > 0:
                return self.redirect("/login")
            error = ""
            if not username:
                error = "Username is required."
            elif len(password) < MIN_PASSWORD_LEN:
                error = f"Password must be at least {MIN_PASSWORD_LEN} characters."
            elif password != data.get("confirm", ""):
                error = "Passwords do not match."
            if error:
                return self.send_html(render_auth_page(setup=True, error=error, username=username), status=400)
            create_user(username, password, role=ROLE_APPROVER)
        else:
            outcome = try_login(username, password, self.client_ip())
            if outcome == "locked":
                return self.send_html(render_auth_page(setup=False, error=FLASH_MESSAGES["login_locked"][1], username=username), status=429)
            if outcome != "ok":
                return self.send_html(render_auth_page(setup=False, error="Invalid username or password.", username=username), status=401)

        token = create_session(username)
        return self.redirect("/", cookie=self.session_cookie(token, SESSION_HOURS * 3600))
