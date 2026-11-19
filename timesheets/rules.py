"""ICAP student timesheets (monthly daily diary)."""

from datetime import date
from datetime import datetime
import os
import secrets

import config
from attendance.records import month_holidays
from auth.roles import ROLE_APPROVER, ROLE_REVIEWER
from auth.sessions import approver_only_emps
from common.constants import ARTICLE_TYPES, MAX_UPLOAD_BYTES, SCAN_TYPES, STUDY_LEAVE_TYPE, TS_CODE_FIELDS, TS_DAY_TYPES, TS_TEXT_FIELDS
from common.dates import first_of_month, fmt_date, month_days, parse_date
from common.errors import ChangeError
from common.numbers import checked_number
from common.settings import is_month_locked

def default_day_type(d, holidays):
    if fmt_date(d) in holidays:
        return "Public Holiday"
    return "Weekend" if d.weekday() >= 5 else "Work"

def timesheet_holiday_work(conn, ts):
    """Diary days marked Work (with hours) on a gazetted holiday: the client didn't allow the holiday."""
    return conn.execute("""SELECT e.work_date, h.name FROM timesheet_entries e JOIN holidays h ON h.holiday_date = e.work_date
                           WHERE e.timesheet_id = ? AND e.day_type = 'Work' AND e.hours > 0 ORDER BY e.work_date""", (ts["id"],)).fetchall()

def timesheet_start(staff_row, cfg):
    """First payroll month this person must submit a timesheet for, or None: every ICAP student, plus other staff
    only from the month the reviewer asked them to."""
    base = cfg.get("timesheet_required_from", "2026-11-01")
    if staff_row["staff_type"] in ARTICLE_TYPES:
        return base
    return max(base, staff_row["timesheet_from"]) if staff_row["timesheet_from"] else None

def timesheet_required(staff_row, pm_str, cfg):
    start = timesheet_start(staff_row, cfg)
    return start is not None and pm_str >= start

def timesheet_mode(cfg, pm_str):
    return "Daily" if pm_str >= cfg.get("timesheet_online_from", "2026-12-01") else "Scanned"

def daily_hours(cfg):
    return float(cfg.get("timesheet_hours_per_day", 8))  # 9-hour office day less the 1-hour lunch break

def timesheet_codes(conn):
    """category -> [(code, description)] for the daily diary drop-downs."""
    codes = {c: [] for c in TS_CODE_FIELDS}
    for r in conn.execute("SELECT category, code, description FROM timesheet_codes ORDER BY category, code"):
        codes.setdefault(r["category"], []).append((r["code"], r["description"] or ""))
    return codes

def get_timesheet(conn, emp_id, pm_str):
    return conn.execute("SELECT * FROM timesheets WHERE emp_id = ? AND payroll_month = ?", (emp_id, pm_str)).fetchone()

def register_leave_days(conn, emp_id, pm_str):
    """Days in the leave register falling in this calendar month (total, study), to cross-check against the diary."""
    days = month_days(pm_str)
    first, last = days[0], days[-1]
    total = study = 0.0
    for l in conn.execute("""SELECT leave_type, date_from, date_to, no_of_days FROM leave_register
                             WHERE emp_id = ? AND status NOT IN ('Rejected', 'Cancelled')""", (emp_id,)):
        f, t = parse_date(l["date_from"]), parse_date(l["date_to"])
        if not f or not t or t < first or f > last:
            continue
        n = min(float(l["no_of_days"]), (min(t, last) - max(f, first)).days + 1)
        total += n
        if l["leave_type"] == STUDY_LEAVE_TYPE:
            study += n
    return total, study

def save_timesheet(conn, emp_id, pm_str, data, files, submit, user, cfg):
    """Create or update the student's diary for the month; optionally submit it. Returns a FLASH_MESSAGES key."""
    staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
    if not staff:
        raise ChangeError("emp_missing")
    if not timesheet_required(staff, pm_str, cfg):
        raise ChangeError("ts_not_icap")
    ts = get_timesheet(conn, emp_id, pm_str)
    if ts and ts["status"] not in ("Draft", "Returned"):
        raise ChangeError("ts_locked")
    mode = timesheet_mode(cfg, pm_str)
    now = datetime.now().isoformat(timespec="seconds")
    if ts is None:
        conn.execute("INSERT INTO timesheets (emp_id, payroll_month, mode, prepared_by) VALUES (?,?,?,?)", (emp_id, pm_str, mode, user))
        ts = get_timesheet(conn, emp_id, pm_str)
    fields = {"remarks": data.get("remarks", "").strip(), "prepared_by": user}

    if mode == "Scanned":
        for col in ("total_hours", "leave_days", "study_days"):
            fields[col] = checked_number(data.get(col))
            if fields[col] < 0:
                raise ChangeError("bad_input")
        upload = files.get("scan")
        if upload and not upload[2]:
            upload = None  # file input left empty
        if submit and ((not upload and not ts["scan_file"]) or fields["total_hours"] <= 0):
            raise ChangeError("ts_need_scan")
        if submit and pm_str > fmt_date(first_of_month(date.today())):
            raise ChangeError("ts_future")
        if upload:
            name, _, content = upload
            kind = next((k for k in SCAN_TYPES if content.startswith(k[0])), None)
            if kind is None or len(content) > MAX_UPLOAD_BYTES:
                raise ChangeError("ts_bad_file")
            os.makedirs(config.UPLOAD_DIR, exist_ok=True)
            stored = f"timesheet_{emp_id}_{pm_str[:7]}_{secrets.token_hex(8)}.{kind[2]}"
            with open(os.path.join(config.UPLOAD_DIR, stored), "wb") as f:
                f.write(content)
            old = ts["scan_file"]
            if old and os.path.exists(os.path.join(config.UPLOAD_DIR, old)):
                os.remove(os.path.join(config.UPLOAD_DIR, old))
            fields.update(scan_file=stored, scan_name=os.path.basename(name.replace("\\", "/"))[:120], scan_type=kind[1])
    else:
        conn.execute("DELETE FROM timesheet_entries WHERE timesheet_id = ?", (ts["id"],))
        valid_codes = {c: {code for code, _ in lst} for c, lst in timesheet_codes(conn).items()}
        holidays = month_holidays(conn, pm_str)
        total = std = leave = study = 0.0
        worked_days = 0
        for d in month_days(pm_str):
            k = d.day
            day_type = data.get(f"type_{k}", "")
            if day_type not in TS_DAY_TYPES:
                day_type = default_day_type(d, holidays)
            if (day_type == "Public Holiday" and fmt_date(d) not in holidays) or (day_type == "Weekend" and d.weekday() < 5):
                raise ChangeError("ts_bad_day")  # holidays come only from the reviewer's gazetted list
            hours = checked_number(data.get(f"hours_{k}"), "ts_bad_hours") if day_type == "Work" else 0.0
            if not 0 <= hours <= 24:
                raise ChangeError("ts_bad_hours")
            texts = {c: data.get(f"{c}_{k}", "").strip()[:200] for c in TS_TEXT_FIELDS} if day_type == "Work" else {c: "" for c in TS_TEXT_FIELDS}
            if any(texts[c] and texts[c] not in valid_codes.get(c, ()) for c in TS_CODE_FIELDS):
                raise ChangeError("ts_bad_code")
            conn.execute(f"""INSERT INTO timesheet_entries (timesheet_id, work_date, day_type, hours, {', '.join(TS_TEXT_FIELDS)})
                             VALUES (?,?,?,?,{', '.join('?' * len(TS_TEXT_FIELDS))})""",
                         (ts["id"], fmt_date(d), day_type, hours, *texts.values()))
            if d.weekday() < 5 and fmt_date(d) not in holidays:  # office working days, even if worked on a holiday / weekend
                std += daily_hours(cfg)
            total += hours
            leave += day_type in ("Leave", "Absent")
            study += day_type == "Study Leave"
            if day_type == "Work" and hours > 0 and texts["client_name"] and texts["nature_of_work"]:
                worked_days += 1
        fields.update(total_hours=total, standard_hours=std, leave_days=leave, study_days=study)
        if submit and worked_days == 0:
            raise ChangeError("ts_need_entries")

    if submit:
        if pm_str > fmt_date(first_of_month(date.today())):
            raise ChangeError("ts_future")
        fields.update(status="Submitted", submitted_at=now)
    conn.execute(f"UPDATE timesheets SET {', '.join(f'{c} = ?' for c in fields)} WHERE id = ?", (*fields.values(), ts["id"]))
    return "ts_submitted" if submit else "ts_saved"

def decide_timesheet(conn, ts, decision, note, user, role, own_emp):
    """Reviewer: Submitted -> Reviewed. Approver: Reviewed -> Approved. Either can return a sheet not yet approved;
    the approver can also reopen an approved one while its payroll month is still Draft. Sheets of staff whose portal the
    reviewer operates skip his review: the approver approves them straight from Submitted."""
    approver_only = ts["emp_id"] in approver_only_emps(conn)
    if (own_emp and ts["emp_id"] == own_emp) or (role == ROLE_REVIEWER and approver_only):
        raise ChangeError("ts_own")
    st = ts["status"]
    allowed = {
        "Reviewed": role == ROLE_REVIEWER and st == "Submitted",
        "Approved": role == ROLE_APPROVER and (st == "Reviewed" or (st == "Submitted" and approver_only)),
        "Returned": st in ("Submitted", "Reviewed") or (st == "Approved" and role == ROLE_APPROVER
                                                        and not is_month_locked(conn, ts["payroll_month"])),
    }.get(decision, False)
    if not allowed:
        raise ChangeError("ts_wrong_stage")
    now = datetime.now().isoformat(timespec="seconds")
    if decision == "Reviewed":
        conn.execute("UPDATE timesheets SET status = 'Reviewed', reviewed_by = ?, reviewed_at = ? WHERE id = ?", (user, now, ts["id"]))
    if decision == "Approved":
        conn.execute("UPDATE timesheets SET status = 'Approved', approved_by = ?, approved_at = ? WHERE id = ?", (user, now, ts["id"]))
    elif decision == "Returned":
        if not note.strip():
            raise ChangeError("ts_need_note")
        conn.execute("""UPDATE timesheets SET status = 'Returned', return_note = ?, returned_by = ?, reviewed_by = NULL, reviewed_at = NULL,
                        approved_by = NULL, approved_at = NULL WHERE id = ?""", (note.strip()[:500], user, ts["id"]))
    return {"Reviewed": "ts_reviewed", "Approved": "ts_approved", "Returned": "ts_returned"}[decision]
