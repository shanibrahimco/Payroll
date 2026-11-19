"""Mobile check-in and approved locations."""

from datetime import date
from datetime import datetime
import math

from common.constants import EOBI_ONLY_TYPE
from common.errors import ChangeError

LOCATION_KINDS = ("Office", "Client Site")

DEFAULT_LOCATION_RADIUS_M = 150

def valid_coordinates(lat, lng):
    return (not isinstance(lat, bool) and not isinstance(lng, bool) and isinstance(lat, (int, float)) and isinstance(lng, (int, float))
            and math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180)

def distance_metres(lat1, lng1, lat2, lng2):
    """Great-circle distance between two points (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6371000 * math.asin(min(1.0, math.sqrt(a)))

def match_work_location(conn, lat, lng):
    """(approved location the point is inside, or None; metres to the nearest approved location, or None if there are none)."""
    sites = [(distance_metres(lat, lng, s["lat"], s["lng"]), s) for s in conn.execute("SELECT * FROM work_locations WHERE active = 1").fetchall()]
    inside = [(m, s) for m, s in sites if m <= s["radius_m"]]
    return (min(inside, key=lambda x: x[0])[1] if inside else None), (min(m for m, _ in sites) if sites else None)

def record_punch(conn, staff, kind, lat, lng, note):
    """Today's mobile check-in / check-out, timed by the server's clock. A check-in at an approved location is Verified; one made
    elsewhere needs the place named and waits for the reviewer. The mobile check-in prevails over any report for the day."""
    if staff["status"] != "Active" or staff["staff_type"] == EOBI_ONLY_TYPE:
        raise ChangeError("not_employee")
    if kind not in ("in", "out"):
        raise ChangeError("bad_input")
    if not valid_coordinates(lat, lng):
        raise ChangeError("bad_location")
    emp_id, today, clock = staff["emp_id"], date.today().isoformat(), datetime.now().strftime("%H:%M")
    note = str(note or "").strip()[:120]
    row = conn.execute("SELECT * FROM attendance WHERE emp_id = ? AND att_date = ?", (emp_id, today)).fetchone()
    if kind == "out":
        if row is None or not row["check_in_time"]:
            raise ChangeError("not_checked_in")
        if row["check_out_time"]:
            raise ChangeError("already_checked_out")
        conn.execute("UPDATE attendance SET check_out_time = ?, out_lat = ?, out_lng = ? WHERE emp_id = ? AND att_date = ?",
                     (clock, lat, lng, emp_id, today))
        return "punched_out"
    if row is not None and row["check_in_time"]:
        raise ChangeError("already_checked_in")
    site, nearest = match_work_location(conn, lat, lng)
    if site is None and not note:
        raise ChangeError("location_need_note")
    conn.execute("""INSERT INTO attendance (emp_id, att_date, status, arrival_time, check_in_time, source, in_lat, in_lng, location_note, location_status, distance_m)
                    VALUES (?,?,'P',?,?,'Mobile',?,?,?,?,?)
                    ON CONFLICT(emp_id, att_date) DO UPDATE SET status = 'P', arrival_time = excluded.arrival_time, check_in_time = excluded.check_in_time,
                        source = 'Mobile', in_lat = excluded.in_lat, in_lng = excluded.in_lng, location_note = excluded.location_note,
                        location_status = excluded.location_status, distance_m = excluded.distance_m""",
                 (emp_id, today, clock, clock, lat, lng, site["name"] if site else note, "Verified" if site else "Review",
                  None if site or nearest is None else round(nearest)))
    return "punched_in" if site else "punched_in_review"
