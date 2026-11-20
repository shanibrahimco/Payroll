"""Email: staff addresses, login details and notices."""

from datetime import date
from datetime import datetime
import csv

import config
from attendance.records import name_key
from auth.roles import ROLE_APPROVER, ROLE_PREPARER, ROLE_REVIEWER
from common.constants import EOBI_ONLY_TYPE
from common.dates import fmt_date, parse_date
from common.errors import ChangeError
from common.settings import get_settings_dict

def notify_approver(conn, sender, subject, body):
    """Email the approver (address set on the Email tab) from the sender's own account, if both are set up.
    The in-app Approvals tab always shows the item; a failed email never blocks the action."""
    to = get_settings_dict(conn).get("approver_notify_email", "")
    account = conn.execute("SELECT * FROM mail_accounts WHERE username = ?", (sender,)).fetchone()
    if not (to and valid_email(to) and account):
        return
    (_, err), = send_emails(account, [(to, subject, body)])
    conn.execute("INSERT INTO email_log (sent_at, sent_by, from_address, to_address, purpose, status, error) VALUES (?,?,?,?,?,?,?)",
                 (datetime.now().isoformat(timespec="seconds"), sender, account["from_address"], to, "Approver notice",
                  "Failed" if err else "Sent", err))

EMAIL_ROLES = (ROLE_APPROVER, ROLE_REVIEWER, ROLE_PREPARER)

MAIL_SECURITY = ("STARTTLS", "SSL", "None")

def valid_email(addr):
    import re
    return bool(re.fullmatch(r"[^@\s,;<>\"']+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", addr or ""))

def starting_password(name):
    """Full name, letters only, first letter capital and the rest small, then @123 (the firm's starting-password rule)."""
    letters = "".join(ch for ch in name if ch.isalpha())
    return letters[:1].upper() + letters[1:].lower() + "@123"

def read_email_list(filename, content):
    """Rows of the uploaded list as lists of strings: .xlsx via openpyxl, otherwise CSV."""
    if filename.lower().endswith(".xlsx") or content.startswith(b"PK\x03\x04"):
        import io
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True).worksheets[0]
        return [["" if v is None else str(v).strip() for v in row] for row in ws.iter_rows(values_only=True)]
    text = content.decode("utf-8-sig", "replace")
    return [[c.strip() for c in row] for row in csv.reader(text.splitlines())]

def import_staff_emails(conn, rows):
    """Match each row to a staff member by Emp ID, CNIC or exact name and store the email. Returns a report dict."""
    header_at = next((i for i, r in enumerate(rows[:10]) if any("mail" in c.lower() for c in r)), None)
    if header_at is None:
        raise ChangeError("email_list_invalid")
    header = [c.lower() for c in rows[header_at]]
    col = lambda *words: next((i for i, h in enumerate(header) if any(w in h for w in words)), None)
    c_email, c_emp, c_cnic, c_name = col("mail"), col("emp", "code"), col("cnic", "nic"), col("name")
    staff = conn.execute("SELECT emp_id, employee_name, cnic FROM staff").fetchall()
    by_emp = {s["emp_id"].upper(): s for s in staff}
    by_cnic = {(s["cnic"] or "").replace("-", ""): s for s in staff if s["cnic"]}
    by_name = {}
    for s in staff:
        by_name.setdefault(name_key(s["employee_name"]), []).append(s)
    report = {"updated": [], "unmatched": [], "invalid": []}
    for r in rows[header_at + 1:]:
        cell = lambda i: r[i] if i is not None and i < len(r) else ""
        email, label = cell(c_email).strip(), " / ".join(x for x in (cell(c_emp), cell(c_cnic), cell(c_name)) if x)
        if not email and not label:
            continue
        s = (by_emp.get(cell(c_emp).strip().upper()) or by_cnic.get(cell(c_cnic).replace("-", "").replace(" ", ""))
             or (lambda m: m[0] if len(m) == 1 else None)(by_name.get(name_key(cell(c_name)), [])))
        if s is None:
            report["unmatched"].append(label or email)
        elif not valid_email(email):
            report["invalid"].append(f"{s['emp_id']} {s['employee_name']}: '{email}'")
        else:
            conn.execute("UPDATE staff SET email = ? WHERE emp_id = ?", (email.lower(), s["emp_id"]))
            report["updated"].append(f"{s['emp_id']} {s['employee_name']}")
    return report

def login_email_text(name, username, password, app_url):
    return (f"Dear {name},\n\n"
            f"Your login for the {config.FIRM_NAME} payroll system is ready.\n\n"
            f"  Address:   {app_url}\n"
            f"  User ID:   {username}\n"
            f"  Password:  {password}\n\n"
            "When you log in for the first time you will be asked to change this password to one of your own.\n"
            "Please do not share your password with anyone.\n\n"
            f"Regards,\n{config.FIRM_NAME}\n")

def send_emails(account, messages):
    """Send [(to, subject, body)] over one SMTP connection from the sender's account. Returns [(to, error or None)]."""
    import smtplib
    import ssl
    from email.message import EmailMessage
    from email.utils import formataddr
    results = []
    try:
        ctx = ssl.create_default_context()
        if account["security"] == "SSL":
            server = smtplib.SMTP_SSL(account["smtp_host"], account["smtp_port"], timeout=20, context=ctx)
        else:
            server = smtplib.SMTP(account["smtp_host"], account["smtp_port"], timeout=20)
        with server:
            if account["security"] == "STARTTLS":
                server.starttls(context=ctx)
            if account["smtp_user"]:
                server.login(account["smtp_user"], account["smtp_password"] or "")
            for to, subject, body in messages:
                msg = EmailMessage()
                msg["From"] = formataddr((account["from_name"] or "", account["from_address"]))
                msg["To"], msg["Subject"] = to, subject
                msg.set_content(body)
                try:
                    server.send_message(msg)
                    results.append((to, None))
                except Exception as e:  # one bad address shouldn't stop the rest
                    results.append((to, str(e)[:200]))
    except Exception as e:  # couldn't connect or log in: every message fails with the same reason
        sent = {to for to, _ in results}
        results += [(to, f"{type(e).__name__}: {str(e)[:180]}") for to, _, _ in messages if to not in sent]
    return results

def email_holiday_notice(conn, user, now, headline=""):
    """Email the upcoming gazetted holidays to all active staff from the user's own account.
    Returns 'sent', 'partly', 'failed', 'no_account', 'no_staff' or 'nothing'."""
    account = conn.execute("SELECT * FROM mail_accounts WHERE username = ?", (user,)).fetchone()
    if account is None:
        return "no_account"
    today = date.today()
    upcoming = conn.execute("SELECT holiday_date, name FROM holidays WHERE holiday_date >= ? AND holiday_date <= ? ORDER BY holiday_date",
                            (fmt_date(today), f"{today.year + 1}-12-31")).fetchall()
    recipients = conn.execute("""SELECT emp_id, employee_name, email FROM staff WHERE status = 'Active' AND COALESCE(email, '') != ''
                                 AND COALESCE(staff_type, '') != ?""", (EOBI_ONLY_TYPE,)).fetchall()
    if not recipients:
        return "no_staff"
    if not upcoming and not headline:
        return "nothing"
    listing = "\n".join(f"  {parse_date(h['holiday_date']).strftime('%a %d %b %Y')}   {h['name']}" for h in upcoming)
    body = ("Dear {name},\n\n" + (headline + "\n\n" if headline else "")
            + ("The gazetted holidays for the office are:\n\n" + listing if upcoming else "There are no other upcoming gazetted holidays.") +
            "\n\nSaturday and Sunday remain off. If you are needed at work on a holiday or a weekend, you will be called.\n"
            "Staff working at a client follow the client's holidays. If you work on a gazetted holiday (or are called in on a "
            "weekend), extra leave is credited after review and approval.\n\n"
            f"Regards,\n{account['from_name'] or user}\n{config.FIRM_NAME}\n")
    results = dict(send_emails(account, [(r["email"], f"{config.FIRM_SHORT} — gazetted holidays", body.replace("{name}", r["employee_name"]))
                                         for r in recipients]))
    failed = 0
    for r in recipients:
        err = results.get(r["email"])
        failed += err is not None
        conn.execute("INSERT INTO email_log (sent_at, sent_by, from_address, to_address, emp_id, purpose, status, error) VALUES (?,?,?,?,?,?,?,?)",
                     (now, user, account["from_address"], r["email"], r["emp_id"], "Holiday list", "Failed" if err else "Sent", err))
    return "sent" if not failed else ("failed" if failed == len(recipients) else "partly")

def email_all_staff(conn, user, now, subject, message):
    """Send one general email to every active staff member with an email address, each as their own message from the
    user's own account. Returns (outcome, number of people): outcome is 'sent', 'partly', 'failed', 'no_account' or 'no_staff'."""
    account = conn.execute("SELECT * FROM mail_accounts WHERE username = ?", (user,)).fetchone()
    if account is None:
        return "no_account", 0
    recipients = conn.execute("""SELECT emp_id, employee_name, email FROM staff WHERE status = 'Active' AND COALESCE(email, '') != ''
                                 AND COALESCE(staff_type, '') != ? ORDER BY emp_id""", (EOBI_ONLY_TYPE,)).fetchall()
    if not recipients:
        return "no_staff", 0
    signature = f"\n\nRegards,\n{account['from_name'] or user}\n{config.FIRM_NAME}\n"
    results = dict(send_emails(account, [(r["email"], subject, f"Dear {r['employee_name']},\n\n{message}{signature}") for r in recipients]))
    failed = 0
    for r in recipients:
        err = results.get(r["email"])
        failed += err is not None
        conn.execute("INSERT INTO email_log (sent_at, sent_by, from_address, to_address, emp_id, purpose, status, error) VALUES (?,?,?,?,?,?,?,?)",
                     (now, user, account["from_address"], r["email"], r["emp_id"], "General email", "Failed" if err else "Sent", err))
    return ("sent" if not failed else "failed" if failed == len(recipients) else "partly"), len(recipients)
