"""Office timings and late arrivals."""

from common.constants import DEFAULT_OFF_DAYS
from common.dates import fmt_date, month_days, parse_date

def staff_timing_rows(conn, emp_id):
    """A person's own timings, the most recently started first."""
    return conn.execute("SELECT * FROM staff_timings WHERE emp_id = ? ORDER BY date_from DESC, id DESC", (emp_id,)).fetchall()

def parse_off_days(text):
    return {int(x) for x in (text or "").split(",") if x.strip().isdigit()}

def timing_on(rows, day, cfg):
    """The timing that applies on a date: the person's own if one covers that date, otherwise the firm's."""
    for r in rows:
        if r["date_from"] <= day and (not r["date_to"] or day <= r["date_to"]):
            return {"start": r["start_time"], "end": r["end_time"], "off": parse_off_days(r["off_days"]), "own": True}
    return {"start": cfg.get("office_start_time", "09:00"), "end": cfg.get("office_end_time", "18:00"),
            "off": parse_off_days(DEFAULT_OFF_DAYS), "own": False}

def clock_minutes(t):
    return int(t.split(":")[0]) * 60 + int(t.split(":")[1])

def arrived_late(status, arrival, cfg, timing=None):
    """Late = arriving more than the grace minutes after the person's start time. With no time in the report, the machine's
    P-LT mark is used for firm timing only: the machine doesn't know a person's own timing."""
    status = (status or "").upper()
    if not status.startswith("P"):
        return False
    if not arrival:
        return "LT" in status and not (timing and timing["own"])
    start = timing["start"] if timing else cfg.get("office_start_time", "09:00")
    return clock_minutes(arrival) > clock_minutes(start) + int(float(cfg.get("late_grace_minutes", 15)))

def late_arrival_days(conn, emp_id, pm_str, cfg):
    """[(date, arrival time)] of the late arrivals in this calendar month that count towards the salary hold.
    The person's off days and gazetted holidays are not working days, so they don't count."""
    days = month_days(pm_str)
    first = max(fmt_date(days[0]), cfg.get("late_hold_from", "2026-11-26"))
    holidays = {r["holiday_date"] for r in conn.execute("SELECT holiday_date FROM holidays")}
    timings = staff_timing_rows(conn, emp_id)
    late = []
    for r in conn.execute("SELECT att_date, status, arrival_time FROM attendance WHERE emp_id = ? AND att_date >= ? AND att_date <= ? ORDER BY att_date",
                          (emp_id, first, fmt_date(days[-1]))).fetchall():
        timing = timing_on(timings, r["att_date"], cfg)
        if (r["att_date"] not in holidays and parse_date(r["att_date"]).weekday() not in timing["off"]
                and arrived_late(r["status"], r["arrival_time"], cfg, timing)):
            late.append((r["att_date"], r["arrival_time"]))
    return late

def late_days_text(late_days):
    return ", ".join(parse_date(d).strftime("%d %b") + (f" ({t})" if t else "") for d, t in late_days)
