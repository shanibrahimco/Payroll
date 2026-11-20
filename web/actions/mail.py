"""Email accounts, staff addresses and login emails."""

import json
import os

import config
from auth.users import hash_password
from common.errors import ChangeError
from common.settings import log_decision
from notifications.mailer import (
    MAIL_SECURITY,
    email_all_staff,
    import_staff_emails,
    login_email_text,
    read_email_list,
    send_emails,
    starting_password,
    valid_email,
)

def handle(self, conn, path, data, ym, pm_str, user, role, own_emp, cfg, cutoff, km_rate, now, enters_for_others):
    if path == "/set_approver_email":
        addr = data.get("approver_email", "").strip().lower()
        if addr and not valid_email(addr):
            raise ChangeError("email_invalid")
        conn.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('approver_notify_email', ?, 'Approver is emailed here when something awaits approval')",
                     (addr,))
        return "approver_email_saved"

    if path == "/email_all_staff":
        subject = " ".join(data.get("subject", "").split())[:150]
        message = data.get("message", "").replace("\r\n", "\n").strip()[:5000]
        if not subject or not message:
            raise ChangeError("general_email_invalid")
        outcome, _count = email_all_staff(conn, user, now, subject, message)
        if outcome == "no_account":
            raise ChangeError("mail_no_account")
        if outcome == "no_staff":
            raise ChangeError("general_email_none")
        return {"sent": "general_email_sent", "partly": "general_email_partly", "failed": "mail_failed"}[outcome]

    if path == "/upload_emails":
        upload = self.files.get("email_list")
        if not upload or not upload[2]:
            raise ChangeError("email_list_invalid")
        try:
            rows = read_email_list(upload[0], upload[2])
        except Exception:
            raise ChangeError("email_list_invalid")
        report = import_staff_emails(conn, rows)
        report.update(by=user, at=now, file=os.path.basename(upload[0].replace("\\", "/"))[:80])
        conn.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('last_email_upload', ?, 'Result of the last staff email list upload')",
                     (json.dumps(report),))
        log_decision(conn, "staff_email", None, None, {"updated": len(report["updated"])},
                     f"Email list '{report['file']}' uploaded: {len(report['updated'])} updated, {len(report['unmatched'])} not matched, "
                     f"{len(report['invalid'])} invalid", user)
        return "emails_uploaded"

    if path == "/set_email":
        emp_id, email = data.get("emp_id", "").strip().upper(), data.get("email", "").strip().lower()
        staff = conn.execute("SELECT employee_name, email FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
        if staff is None:
            raise ChangeError("emp_missing")
        if email and not valid_email(email):
            raise ChangeError("email_invalid")
        conn.execute("UPDATE staff SET email = ? WHERE emp_id = ?", (email or None, emp_id))
        log_decision(conn, "staff_email", emp_id, None, {"email": email},
                     f"{emp_id} {staff['employee_name']}: email '{staff['email'] or ''}' → '{email}'", user)
        return "email_saved"

    if path == "/save_mail_account":
        existing = conn.execute("SELECT smtp_password FROM mail_accounts WHERE username = ?", (user,)).fetchone()
        addr, host = data.get("from_address", "").strip(), data.get("smtp_host", "").strip()
        security = data.get("security")
        try:
            port = int(data.get("smtp_port") or 0)
        except ValueError:
            port = 0
        if not valid_email(addr) or not host or not 0 < port < 65536 or security not in MAIL_SECURITY:
            raise ChangeError("mail_account_invalid")
        pw = data.get("smtp_password") or (existing["smtp_password"] if existing else "")  # blank keeps the saved one
        conn.execute("""INSERT OR REPLACE INTO mail_accounts (username, from_address, from_name, smtp_host, smtp_port, security, smtp_user, smtp_password, updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                     (user, addr, data.get("from_name", "").strip()[:80], host, port, security,
                      data.get("smtp_user", "").strip() or addr, pw, now))
        return "mail_account_saved"

    if path == "/set_app_url":
        url = data.get("app_url", "").strip()
        if not url.startswith(("http://", "https://")):
            raise ChangeError("bad_input")
        conn.execute("UPDATE settings SET value = ? WHERE key = 'app_url'", (url[:200],))
        return "app_url_saved"

    if path in ("/send_test_email", "/send_logins"):
        account = conn.execute("SELECT * FROM mail_accounts WHERE username = ?", (user,)).fetchone()
        if account is None:
            raise ChangeError("mail_no_account")
        if path == "/send_test_email":
            (to, err), = send_emails(account, [(account["from_address"], f"{config.FIRM_SHORT} Payroll — test email",
                                                f"This is a test email from the {config.FIRM_NAME} payroll system, sent by {user}.\n")])
            conn.execute("INSERT INTO email_log (sent_at, sent_by, from_address, to_address, purpose, status, error) VALUES (?,?,?,?,?,?,?)",
                         (now, user, account["from_address"], to, "Test", "Failed" if err else "Sent", err))
            return "mail_failed" if err else "mail_test_sent"
        # login details: only accounts still on their starting password; reset it to the rule so the email is right
        wanted = data.get("emp_id", "").strip().upper()
        targets = conn.execute("""
                SELECT u.username, u.emp_id, s.employee_name, s.email FROM users u JOIN staff s ON s.emp_id = u.emp_id
                WHERE u.must_change_pw = 1 AND COALESCE(s.email, '') != '' AND (? = '' OR u.emp_id = ?) ORDER BY u.emp_id
            """, (wanted, wanted)).fetchall()
        if not targets:
            raise ChangeError("logins_none")
        app_url = cfg.get("app_url", f"http://127.0.0.1:{config.PORT}")
        messages, passwords = [], {}
        for t in targets:
            passwords[t["username"]] = starting_password(t["employee_name"])
            messages.append((t["email"], f"Your login for the {config.FIRM_SHORT} payroll system",
                             login_email_text(t["employee_name"], t["username"], passwords[t["username"]], app_url)))
        for t in targets:  # the emailed password must be the one that works
            salt_hex, hash_hex = hash_password(passwords[t["username"]])
            conn.execute("UPDATE users SET password_salt = ?, password_hash = ?, must_change_pw = 1 WHERE username = ?",
                         (salt_hex, hash_hex, t["username"]))
        results = dict(send_emails(account, messages))
        failed = 0
        for t in targets:
            err = results.get(t["email"])
            failed += err is not None
            conn.execute("INSERT INTO email_log (sent_at, sent_by, from_address, to_address, emp_id, purpose, status, error) VALUES (?,?,?,?,?,?,?,?)",
                         (now, user, account["from_address"], t["email"], t["emp_id"], "Login details", "Failed" if err else "Sent", err))
        return "logins_sent" if not failed else ("mail_failed" if failed == len(targets) else "logins_partly")
