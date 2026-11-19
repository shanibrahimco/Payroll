"""Attendance, locations, timings and part-time hours."""

from datetime import date
import json
import os
import secrets

import config
from attendance.mobile import DEFAULT_LOCATION_RADIUS_M, LOCATION_KINDS, valid_coordinates
from attendance.records import attendance_pdf_text, attendance_summary, parse_attendance_text, store_attendance
from auth.roles import ROLE_REVIEWER
from auth.sessions import approver_only_emps
from common.constants import ABSENCE_LEAVE_TYPE, MAX_UPLOAD_BYTES, STAFF_LEAVE_TYPES, WEEKDAY_NAMES
from common.dates import first_of_month, fmt_date, get_payroll_month_for_event, parse_date, pay_month_label
from common.errors import ChangeError
from common.numbers import checked_number
from common.settings import is_month_locked, log_decision
from leaves.rules import check_entry
from payroll.calculator import weekly_hours_for
from payroll.changes import normalise_hours

def handle(self, conn, path, data, ym, pm_str, user, role, own_emp, cfg, cutoff, km_rate, now, enters_for_others):
    if path == "/upload_attendance":
        upload = self.files.get("report")
        if not upload or not upload[2] or not upload[2].startswith(b"%PDF") or len(upload[2]) > MAX_UPLOAD_BYTES:
            raise ChangeError("attendance_unreadable")
        try:
            period_from, period_to, month, people = parse_attendance_text(attendance_pdf_text(upload[2]))
        except Exception:
            raise ChangeError("attendance_unreadable")
        if not people or not month:
            raise ChangeError("attendance_unreadable")
        report_month = f"{month}-01"
        name = os.path.basename(upload[0].replace("\\", "/"))[:120]
        folder = os.path.join(config.UPLOAD_DIR, "attendance")
        os.makedirs(folder, exist_ok=True)
        stored = f"{month}_{secrets.token_hex(6)}.pdf"
        with open(os.path.join(folder, stored), "wb") as f:
            f.write(upload[2])
        cur = conn.execute("""INSERT INTO attendance_uploads (payroll_month, file_name, stored_file, period_from, period_to, uploaded_by, uploaded_at, parsed)
                                  VALUES (?,?,?,?,?,?,?,?)""", (report_month, name, stored, period_from, period_to, user, now, json.dumps(people)))
        unmatched = store_attendance(conn, cur.lastrowid, people)
        conn.execute("UPDATE attendance_uploads SET unmatched = ? WHERE id = ?", (json.dumps(unmatched), cur.lastrowid))
        log_decision(conn, "attendance", None, report_month, {"upload": cur.lastrowid},
                     f"Attendance report '{name}' ({period_from} to {period_to}): {len(people) - len(unmatched)} staff matched"
                     + (f", not matched: {', '.join(unmatched)}" if unmatched else ""), user)
        return "attendance_partly" if unmatched else "attendance_uploaded"

    if path == "/map_attendance":
        code, emp_id = data.get("code", "").strip(), data.get("emp_id", "").strip().upper()
        if not code or not conn.execute("SELECT 1 FROM staff WHERE emp_id = ?", (emp_id,)).fetchone():
            raise ChangeError("emp_missing")
        conn.execute("UPDATE staff SET attendance_code = NULL WHERE attendance_code = ?", (code,))
        conn.execute("UPDATE staff SET attendance_code = ? WHERE emp_id = ?", (code, emp_id))
        for up in conn.execute("SELECT id, parsed FROM attendance_uploads ORDER BY id").fetchall():  # re-apply every report
            people = json.loads(up["parsed"])
            still = store_attendance(conn, up["id"], people)
            conn.execute("UPDATE attendance_uploads SET unmatched = ? WHERE id = ?", (json.dumps(still), up["id"]))
        return "attendance_mapped"

    if path == "/save_location":
        name, kind = data.get("name", "").strip()[:80], data.get("kind")
        try:
            lat, lng = float(data.get("lat", "")), float(data.get("lng", ""))
            radius = float(data.get("radius_m") or DEFAULT_LOCATION_RADIUS_M)
        except ValueError:
            raise ChangeError("location_invalid")
        if not name or kind not in LOCATION_KINDS or not valid_coordinates(lat, lng) or not 30 <= radius <= 2000:
            raise ChangeError("location_invalid")
        conn.execute("INSERT INTO work_locations (name, kind, lat, lng, radius_m, added_by, added_at) VALUES (?,?,?,?,?,?,?)",
                     (name, kind, lat, lng, radius, user, now))
        log_decision(conn, "work_location", None, None, {"name": name, "lat": lat, "lng": lng, "radius_m": radius},
                     f"Check-in location added: {name} ({kind}), radius {radius:,.0f} m", user)
        return "location_saved"

    if path == "/remove_location":
        site = conn.execute("SELECT * FROM work_locations WHERE id = ? AND active = 1", (int(data["id"]),)).fetchone()
        if site is None:
            raise ChangeError("bad_input")
        conn.execute("UPDATE work_locations SET active = 0 WHERE id = ?", (site["id"],))
        log_decision(conn, "work_location", None, None, {"id": site["id"]}, f"Check-in location removed: {site['name']}", user)
        return "location_removed"

    if path == "/set_mobile_start":
        text = data.get("start_date", "").strip()
        start = parse_date(text) if text else None
        if text and start is None:
            raise ChangeError("mobile_start_invalid")
        value = fmt_date(start) if start else ""
        conn.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('mobile_attendance_from', ?, 'Working days from this date with no mobile check-in are listed as absences')",
                     (value,))
        log_decision(conn, "work_location", None, None, {"mobile_attendance_from": value},
                     f"Mobile attendance start date set to {value}" if value else "Mobile attendance start date cleared", user)
        return "mobile_start_saved"

    if path == "/review_punch":
        emp_id, att_date, decision = data.get("emp_id", "").strip().upper(), data.get("att_date", ""), data.get("decision")
        note = data.get("note", "").strip()[:200]
        punch = conn.execute("""SELECT a.*, s.employee_name FROM attendance a JOIN staff s ON s.emp_id = a.emp_id
                                    WHERE a.emp_id = ? AND a.att_date = ?""", (emp_id, att_date)).fetchone()
        if punch is None or decision not in ("Accepted", "Rejected"):
            raise ChangeError("bad_input")
        if punch["location_status"] != "Review":
            raise ChangeError("punch_wrong_stage")
        if (own_emp and emp_id == own_emp) or (role == ROLE_REVIEWER and emp_id in approver_only_emps(conn)):
            raise ChangeError("punch_own")
        if is_month_locked(conn, fmt_date(first_of_month(parse_date(att_date)))):
            raise ChangeError("locked")
        if decision == "Rejected" and not note:
            raise ChangeError("punch_need_note")
        conn.execute("UPDATE attendance SET location_status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ? WHERE emp_id = ? AND att_date = ?",
                     (decision, user, now, note, emp_id, att_date))
        if decision == "Rejected":
            conn.execute("UPDATE attendance SET status = 'A', arrival_time = NULL WHERE emp_id = ? AND att_date = ?", (emp_id, att_date))
        log_decision(conn, "attendance", emp_id, None, {"att_date": att_date, "decision": decision},
                     f"{emp_id} {punch['employee_name']}: check-in on {att_date} at '{punch['location_note']}' {decision.lower()}" + (f" — {note}" if note else ""), user)
        return "punch_accepted" if decision == "Accepted" else "punch_rejected"

    if path == "/absence_action":
        emp_id = data.get("emp_id", "").strip().upper()
        action = data.get("action")
        if own_emp and emp_id == own_emp:
            raise ChangeError("absence_own")
        summary = attendance_summary(conn, pm_str).get(emp_id)
        if summary is None or action not in ("leave", "absence") or not summary["uncovered"]:
            raise ChangeError("bad_input")
        if action == "leave":
            leave_type = data.get("leave_type")  # the preparer chooses which leave the absent days were
            if leave_type not in STAFF_LEAVE_TYPES:
                raise ChangeError("bad_input")
        else:
            leave_type = ABSENCE_LEAVE_TYPE
        for d in summary["uncovered"]:
            pm_target = fmt_date(get_payroll_month_for_event(parse_date(d), cutoff))
            check_entry(conn, emp_id, pm_target)
            conn.execute("""INSERT INTO leave_register (application_date, emp_id, leave_type, date_from, date_to, no_of_days, reason, status,
                                                            payroll_month, entered_by) VALUES (?,?,?,?,?,1,?,?,?,?)""",
                         (date.today().isoformat(), emp_id, leave_type, d, d, "Absent per attendance record",
                          "Pending" if action == "leave" else "Recorded", pm_target, user))
        return "absences_filed" if action == "leave" else "absences_recorded"

    if path == "/save_part_hours":
        emp_id = data.get("emp_id", "").strip().upper()
        staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
        if staff is None:
            raise ChangeError("emp_missing")
        if role == ROLE_REVIEWER and emp_id == own_emp:
            raise ChangeError("hours_own")
        if is_month_locked(conn, pm_str):
            raise ChangeError("locked")
        entered = checked_number(data.get("weekly_hours"), "hours_invalid")
        if entered <= 0:
            raise ChangeError("hours_invalid")
        before = weekly_hours_for(conn, staff, pm_str)
        hours = normalise_hours(entered, cfg)
        note = data.get("note", "").strip()
        conn.execute("""INSERT OR REPLACE INTO part_time_hours (emp_id, payroll_month, weekly_hours, note, set_by, set_at)
                            VALUES (?,?,?,?,?,?)""", (emp_id, pm_str, hours, note, user, now))
        log_decision(conn, "part_time_hours", emp_id, pm_str, {"weekly_hours": hours, "note": note},
                     f"{emp_id} {staff['employee_name']} — {pay_month_label(pm_str)}: weekly hours {before if before else 'full time'} → {hours if hours else 'full time'}"
                     + (f" ({note})" if note else ""), user)
        return "hours_saved"

    if path == "/save_timing":
        import re
        emp_id = data.get("emp_id", "").strip().upper()
        staff = conn.execute("SELECT employee_name FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
        if staff is None:
            raise ChangeError("emp_missing")
        if role == ROLE_REVIEWER and emp_id == own_emp:
            raise ChangeError("timing_own")
        start, end, reason = data.get("start_time", "").strip(), data.get("end_time", "").strip(), data.get("reason", "").strip()
        d_from, d_to = parse_date(data.get("date_from")), parse_date(data.get("date_to")) if data.get("date_to") else None
        off = [i for i in range(7) if data.get(f"off_{i}")]
        valid_time = lambda t: re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t)
        if (not d_from or (data.get("date_to") and (not d_to or d_to < d_from)) or not valid_time(start) or not valid_time(end)
                or start >= end or len(off) == 7 or not reason):
            raise ChangeError("timing_invalid")
        conn.execute("""INSERT INTO staff_timings (emp_id, date_from, date_to, start_time, end_time, off_days, reason, set_by, set_at)
                            VALUES (?,?,?,?,?,?,?,?,?)""", (emp_id, fmt_date(d_from), fmt_date(d_to) if d_to else None, start, end,
                                                        ",".join(map(str, off)), reason, user, now))
        log_decision(conn, "staff_timing", emp_id, None, {"from": fmt_date(d_from), "to": fmt_date(d_to) if d_to else None, "start": start, "end": end, "off": off},
                     f"{emp_id} {staff['employee_name']}: timing {start} to {end}, off {', '.join(WEEKDAY_NAMES[i] for i in off) or 'none'}, "
                     f"from {fmt_date(d_from)}{' to ' + fmt_date(d_to) if d_to else ''} — {reason}", user)
        return "timing_saved"

    if path == "/delete_timing":
        t = conn.execute("""SELECT t.*, s.employee_name FROM staff_timings t JOIN staff s ON s.emp_id = t.emp_id WHERE t.id = ?""",
                         (int(data.get("id") or 0),)).fetchone()
        if t is None:
            raise ChangeError("timing_invalid")
        if role == ROLE_REVIEWER and t["emp_id"] == own_emp:
            raise ChangeError("timing_own")
        conn.execute("DELETE FROM staff_timings WHERE id = ?", (t["id"],))
        log_decision(conn, "staff_timing", t["emp_id"], None, {"removed": t["id"]},
                     f"{t['emp_id']} {t['employee_name']}: timing {t['start_time']} to {t['end_time']} from {t['date_from']} removed", user)
        return "timing_removed"

    if path == "/release_late_hold":
        emp_id, withheld_month = data["emp_id"], data["withheld_month"]
        line = conn.execute("""SELECT p.salary_withheld_late, p.employee_name, run.status FROM payroll_lines p
                                   JOIN payroll_runs run ON run.payroll_month = p.payroll_month
                                   WHERE p.emp_id = ? AND p.payroll_month = ?""", (emp_id, withheld_month)).fetchone()
        already = conn.execute("SELECT 1 FROM late_hold_releases WHERE emp_id = ? AND withheld_month = ?", (emp_id, withheld_month)).fetchone()
        same_month = withheld_month == pm_str
        if (line is None or already or line["salary_withheld_late"] <= 0 or is_month_locked(conn, pm_str)
                or withheld_month > pm_str or (not same_month and line["status"] != "Approved")):
            raise ChangeError("release_invalid")
        amount = line["salary_withheld_late"]
        note = data.get("note", "").strip()
        conn.execute("""INSERT INTO late_hold_releases (emp_id, withheld_month, amount, release_month, note, released_by, released_at)
                            VALUES (?,?,?,?,?,?,?)""", (emp_id, withheld_month, amount, pm_str, note, user, now))
        log_decision(conn, "late_hold_release", emp_id, pm_str, {"withheld_month": withheld_month, "amount": amount, "note": note},
                     f"{emp_id} {line['employee_name']}: Rs. {amount:,.0f} held in {pay_month_label(withheld_month)} for late arrivals "
                     f"released in {pay_month_label(pm_str)}" + (f" — {note}" if note else ""), user)
        return "late_released"
