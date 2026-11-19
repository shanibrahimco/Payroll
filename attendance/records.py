"""Attendance records, required staff and absences."""

from datetime import date

from attendance.timing import arrived_late, staff_timing_rows, timing_on
from auth.roles import ROLE_APPROVER
from common.constants import EOBI_ONLY_TYPE
from common.dates import fmt_date, month_days, parse_date
from common.settings import get_settings_dict

def month_holidays(conn, pm_str):
    """{YYYY-MM-DD: name} of the gazetted holidays in the calendar month."""
    days = month_days(pm_str)
    return {r["holiday_date"]: r["name"] for r in conn.execute(
        "SELECT holiday_date, name FROM holidays WHERE holiday_date BETWEEN ? AND ?", (fmt_date(days[0]), fmt_date(days[-1])))}

ATT_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}

def parse_attendance_text(text):
    """Blocks of the attendance report: an 'EmpCode Name Present HL WO Absent Leave PaidDays ...' heading, the person's
    line ('112 Staff Name 8 0 2 3 0 10 43:59'), then day-number lines ('01 02 ... 12') each followed later by a
    'Status WO P-LT A ...' line. Returns (period_from, period_to, month 'YYYY-MM' or None, [people])."""
    import re
    lines = [" ".join(l.split()) for l in text.splitlines() if l.strip()]
    m = re.search(r"Report from\s*:\s*(\d{2})-(\d{2})-(\d{4})\s*To\s*:\s*(\d{2})-(\d{2})-(\d{4})", text)
    period = (f"{m.group(3)}-{m.group(2)}-{m.group(1)}", f"{m.group(6)}-{m.group(5)}-{m.group(4)}") if m else (None, None)
    mm = re.search(r"Month Of\s*:?-?\s*([A-Za-z]{3})[a-z]*\s+(\d{4})", text)
    month = f"{mm.group(2)}-{ATT_MONTHS[mm.group(1).lower()]:02d}" if mm and mm.group(1).lower() in ATT_MONTHS else None
    if month is None and period[0]:
        month = period[0][:7]
    people, cur, days, arrivals = [], None, [], []
    person_re = re.compile(r"^(\d+)\s+(.+?)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)(?:\s+(.*))?$")
    for i, line in enumerate(lines):
        if line.startswith("EmpCode") and i + 1 < len(lines):
            pm_ = person_re.match(lines[i + 1])
            if pm_:
                cur = {"code": pm_.group(1), "name": pm_.group(2).strip(), "present": int(pm_.group(3)), "absent": int(pm_.group(6)),
                       "leave": int(pm_.group(7)), "paid_days": int(pm_.group(8)), "extra": (pm_.group(9) or "").strip(), "days": {}, "arrivals": {}}
                people.append(cur)
                days, arrivals = [], []
            continue
        if cur is None:
            continue
        tokens = line.split()
        if tokens and all(re.fullmatch(r"\d{2}", t) for t in tokens) and all(1 <= int(t) <= 31 for t in tokens):
            days = [int(t) for t in tokens]
        elif line.lower().startswith("arrived"):
            arrivals = re.findall(r"\b\d{1,2}:\d{2}\b", line)  # listed only for the days the person came, in date order
        elif line.startswith("Status") and days:
            statuses = tokens[1:]
            if len(statuses) == len(days) and month:
                for d, st in zip(days, statuses):
                    cur["days"][f"{month}-{d:02d}"] = st.upper()
                came = [d for d, st in zip(days, statuses) if st.upper().startswith("P")]
                if len(came) == len(arrivals):
                    cur["arrivals"].update({f"{month}-{d:02d}": t.zfill(5) for d, t in zip(came, arrivals)})
            days, arrivals = [], []
    return period[0], period[1], month, [p for p in people if p["days"]]

def attendance_pdf_text(content):
    import io
    from pypdf import PdfReader
    return "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(content)).pages)

def name_key(name):
    """A name for matching uploaded files: letters only, small, so 'Johnsmith' and 'John Smith' are the same person."""
    return "".join(ch for ch in str(name or "").lower() if ch.isalpha())

def store_attendance(conn, upload_id, people):
    """Match each person to staff (by attendance code, else exact name — the code is then remembered) and save the days.
    Returns the list of people not matched."""
    staff = conn.execute("SELECT emp_id, employee_name, attendance_code FROM staff").fetchall()
    by_code = {s["attendance_code"]: s["emp_id"] for s in staff if s["attendance_code"]}
    by_name = {}
    for s in staff:
        by_name.setdefault(name_key(s["employee_name"]), []).append(s["emp_id"])
    unmatched = []
    for p in people:
        emp = by_code.get(p["code"])
        if emp is None:
            names = by_name.get(name_key(p["name"]), [])
            emp = names[0] if len(names) == 1 else None
            if emp:
                conn.execute("UPDATE staff SET attendance_code = ? WHERE emp_id = ?", (p["code"], emp))
        if emp is None:
            unmatched.append(f"{p['code']} {p['name']}")
            continue
        for d, st in p["days"].items():
            # A mobile check-in always prevails: a report only fills days that have none
            conn.execute("""INSERT INTO attendance (emp_id, att_date, status, upload_id, arrival_time, source) VALUES (?,?,?,?,?,'Biometric')
                            ON CONFLICT(emp_id, att_date) DO UPDATE SET status = excluded.status, upload_id = excluded.upload_id,
                                arrival_time = excluded.arrival_time, source = 'Biometric'
                            WHERE attendance.check_in_time IS NULL""",
                         (emp, d, st, upload_id, p.get("arrivals", {}).get(d)))
    return unmatched

def attendance_required_staff(conn):
    """Everyone who must check in: all active employees, including the reviewer and preparer. Only the approver is exempt."""
    return conn.execute("""SELECT * FROM staff WHERE status = 'Active' AND COALESCE(staff_type, '') != ?
                           AND emp_id NOT IN (SELECT emp_id FROM users WHERE role = ? AND emp_id IS NOT NULL) ORDER BY emp_id""",
                        (EOBI_ONLY_TYPE, ROLE_APPROVER)).fetchall()

def attendance_summary(conn, pm_str):
    """Per staff member with attendance this calendar month: counts, and absent dates not covered by a leave or holiday."""
    days = month_days(pm_str)
    first, last = fmt_date(days[0]), fmt_date(days[-1])
    holidays = month_holidays(conn, pm_str)
    cfg = get_settings_dict(conn)
    out, timings = {}, {}
    for r in conn.execute("""SELECT a.emp_id, a.att_date, a.status, a.arrival_time, s.employee_name FROM attendance a JOIN staff s ON s.emp_id = a.emp_id
                             WHERE a.att_date BETWEEN ? AND ? ORDER BY a.emp_id, a.att_date""", (first, last)).fetchall():
        e = out.setdefault(r["emp_id"], {"name": r["employee_name"], "present": 0, "late": 0, "absent": [], "off": 0, "days": 0})
        st = r["status"]
        e["days"] += 1
        if r["emp_id"] not in timings:
            timings[r["emp_id"]] = staff_timing_rows(conn, r["emp_id"])
        timing = timing_on(timings[r["emp_id"]], r["att_date"], cfg)
        day_off = parse_date(r["att_date"]).weekday() in timing["off"]  # the machine shows some off days as absent
        if st.startswith("P"):
            e["present"] += 1
            e["late"] += not day_off and r["att_date"] not in holidays and arrived_late(st, r["arrival_time"], cfg, timing)
        elif st == "A" and not day_off:
            e["absent"].append(r["att_date"])
        else:
            e["off"] += 1
    start = cfg.get("mobile_attendance_from", "")
    if start:  # attendance is by mobile check-in: a past working day with no check-in is an absence
        seen = {(r["emp_id"], r["att_date"]) for r in conn.execute("SELECT emp_id, att_date FROM attendance WHERE att_date BETWEEN ? AND ?", (first, last))}
        today = date.today().isoformat()
        for s in attendance_required_staff(conn):
            staff_timings = staff_timing_rows(conn, s["emp_id"])
            for d in days:
                ds = fmt_date(d)
                if (ds < start or ds >= today or ds in holidays or (s["emp_id"], ds) in seen or (s["date_of_joining"] or "") > ds
                        or (s["leaving_date"] and s["leaving_date"] < ds) or d.weekday() in timing_on(staff_timings, ds, cfg)["off"]):
                    continue
                e = out.setdefault(s["emp_id"], {"name": s["employee_name"], "present": 0, "late": 0, "absent": [], "off": 0, "days": 0})
                e["days"] += 1
                e["absent"].append(ds)
    for emp, e in out.items():
        e["absent"].sort()
        leaves = conn.execute("""SELECT date_from, date_to FROM leave_register WHERE emp_id = ? AND status NOT IN ('Rejected', 'Cancelled')
                                 AND date_to >= ? AND date_from <= ?""", (emp, first, last)).fetchall()
        def covered(d):
            return d in holidays or any(l["date_from"] <= d <= l["date_to"] for l in leaves)
        e["covered"] = [d for d in e["absent"] if covered(d)]
        e["uncovered"] = [d for d in e["absent"] if not covered(d)]
    return out
