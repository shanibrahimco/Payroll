"""The attendance tab of the web app."""

import html as html_lib

from attendance.mobile import DEFAULT_LOCATION_RADIUS_M, LOCATION_KINDS
from attendance.records import attendance_required_staff, attendance_summary
from attendance.timing import clock_minutes, parse_off_days
from auth.roles import MOBILE_ROLES, ROLE_ACTIONS, ROLE_REVIEWER
from auth.sessions import approver_only_emps
from common.constants import EOBI_ONLY_TYPE, STAFF_LEAVE_TYPES, WEEKDAY_NAMES
from common.dates import fmt_date, month_days, parse_date, pay_month_label
from common.settings import get_settings_dict, is_month_locked
from payroll.calculator import weekly_hours_for

def render(self, conn, own_emp, pm_str, role, ym, body=""):
    esc = html_lib.escape
    can_act = role in ROLE_ACTIONS["/absence_action"]
    summary = attendance_summary(conn, pm_str)
    staff_rows = {s["emp_id"]: s for s in conn.execute("SELECT * FROM staff")}
    can_manage_sites = role in ROLE_ACTIONS["/save_location"]
    not_mine_to_decide = ({own_emp} | approver_only_emps(conn)) if role == ROLE_REVIEWER else {own_emp}
    can_check_in = {r["emp_id"] for r in conn.execute("SELECT emp_id FROM users WHERE emp_id IS NOT NULL AND role IN (?,?,?)", MOBILE_ROLES)}
    no_login = [s for s in attendance_required_staff(conn) if s["emp_id"] not in can_check_in]
    month_dates = month_days(pm_str)
    sites = conn.execute("SELECT * FROM work_locations WHERE active = 1 ORDER BY kind DESC, name").fetchall()
    to_review = conn.execute("""SELECT a.*, s.employee_name FROM attendance a JOIN staff s ON s.emp_id = a.emp_id
                                        WHERE a.location_status = 'Review' AND a.att_date BETWEEN ? AND ? ORDER BY a.att_date, a.emp_id""",
                             (fmt_date(month_dates[0]), fmt_date(month_dates[-1]))).fetchall()

    def site_row(s):
        remove = (f"<form method='POST' action='/remove_location?ym={ym}'><input type='hidden' name='id' value='{s['id']}'>"
                  "<button class='btn btn-sec' type='submit'>Remove</button></form>") if can_manage_sites else ""
        return (f"<tr><td><strong>{esc(s['name'])}</strong></td><td>{esc(s['kind'])}</td><td>{s['lat']:.6f}, {s['lng']:.6f}</td>"
                f"<td class='num'>{s['radius_m']:,.0f} m</td><td>{esc(s['added_by'] or '')}</td><td>{remove}</td></tr>")

    def review_row(a):
        map_url = f"https://www.google.com/maps?q={a['in_lat']:.6f},{a['in_lng']:.6f}"
        away = f"{a['distance_m'] / 1000:,.1f} km from the nearest approved location" if a["distance_m"] is not None else "No approved locations set"
        decide = "<span class='note'>The approver decides this one.</span>" if a["emp_id"] in not_mine_to_decide else ""
        if can_manage_sites and a["emp_id"] not in not_mine_to_decide:
            hidden = f"<input type='hidden' name='emp_id' value='{a['emp_id']}'><input type='hidden' name='att_date' value='{a['att_date']}'>"
            decide = (f"<form method='POST' action='/review_punch?ym={ym}' style='display:inline;'>{hidden}<input type='hidden' name='decision' value='Accepted'>"
                      "<button class='btn' type='submit'>Accept</button></form> "
                      f"<form method='POST' action='/review_punch?ym={ym}' style='display:inline-flex; gap:4px;'>{hidden}<input type='hidden' name='decision' value='Rejected'>"
                      "<input name='note' required placeholder='Reason' size='16'><button class='btn btn-danger' type='submit'>Reject</button></form>")
        return (f"<tr><td>{a['att_date']}</td><td>{a['emp_id']}</td><td><strong>{esc(a['employee_name'])}</strong></td><td>{esc(a['check_in_time'] or '')}</td>"
                f"<td>{esc(a['location_note'] or '')}</td><td>{away}<br><a href='{map_url}' target='_blank' rel='noopener noreferrer'>Open map</a></td><td>{decide}</td></tr>")

    site_form = f"""
              <form class="inline" method="POST" action="/save_location?ym={ym}">
                <label>Name <input name="name" required size="22" placeholder="e.g. Head Office"></label>
                <label>Type <select name="kind">{"".join(f"<option>{k}</option>" for k in LOCATION_KINDS)}</select></label>
                <label>Latitude <input name="lat" required size="11" placeholder="24.860700"></label>
                <label>Longitude <input name="lng" required size="11" placeholder="67.001100"></label>
                <label>Radius (metres) <input type="number" name="radius_m" min="30" max="2000" value="{DEFAULT_LOCATION_RADIUS_M}" style="width:90px;"></label>
                <button class="btn" type="submit">Add Location</button>
              </form>""" if can_manage_sites else ""
    mobile_start = get_settings_dict(conn).get("mobile_attendance_from", "")
    start_form = f"""
              <form class="inline" method="POST" action="/set_mobile_start?ym={ym}">
                <label>Mobile attendance starts on <input type="date" name="start_date" value="{esc(mobile_start, quote=True)}"></label>
                <button class="btn btn-sec" type="submit">Save Start Date</button>
              </form>""" if can_manage_sites else ""
    mobile_panel = f"""
            <div class="panel">
              <h3>Attendance — {pay_month_label(pm_str)}</h3>
              <p class="note"><b>Attendance is by mobile check-in and is mandatory for everyone except the approver</b>, including the reviewer and the preparer.
                From the start date below, a past working day with no check-in, leave or gazetted holiday is listed
                as an absence, to be filed as leave (then recommended by the reviewer and approved by the approver) or recorded as unapproved absence.
                Nobody can act on their own absence or decide their own check-in.
                {'<b>No start date is set, so absences are not being marked yet.</b>' if not mobile_start else ''}</p>
              {start_form}
              {"<div class='flash flash-warn'>These people must check in but have no login linked to them, so they will show as absent until one is added on the Users tab: " + esc(", ".join(s["emp_id"] + " " + s["employee_name"] for s in no_login)) + "</div>" if no_login else ""}
            </div>
            <div class="panel">
              <h3>Check-Ins to Review ({len(to_review)})</h3>
              <p class="note">These people checked in away from every approved location. They count as present unless rejected; a rejected check-in becomes an absence.</p>
              <table><thead><tr><th>Date</th><th>ID</th><th>Name</th><th>Check-In</th><th>Place Given</th><th>Location</th><th></th></tr></thead>
                <tbody>{"".join(review_row(a) for a in to_review) or "<tr><td colspan='7'>Nothing to review.</td></tr>"}</tbody></table>
            </div>
            <div class="panel">
              <h3>Approved Check-In Locations</h3>
              <p class="note">A check-in within a location's radius is accepted automatically. To get the coordinates, right-click the place in Google Maps and click the numbers shown.</p>
              {site_form}
              <table><thead><tr><th>Name</th><th>Type</th><th>Coordinates</th><th class="num">Radius</th><th>Added By</th><th></th></tr></thead>
                <tbody>{"".join(site_row(s) for s in sites) or "<tr><td colspan='6'>No locations yet: every check-in will need review until the office is added.</td></tr>"}</tbody></table>
            </div>"""

    def att_row(emp, a):
        s = staff_rows.get(emp)
        left = s is not None and s["status"] == "Left"
        actions = ""
        if can_act and a["uncovered"] and not left and emp != own_emp:
            type_opts = "".join(f"<option>{t}</option>" for t in STAFF_LEAVE_TYPES)
            actions = (f"<form method='POST' action='/absence_action?ym={ym}' style='display:inline-flex; gap:4px;'>"
                       f"<input type='hidden' name='emp_id' value='{emp}'><input type='hidden' name='action' value='leave'>"
                       f"<select name='leave_type'>{type_opts}</select>"
                       f"<button class='btn btn-sec' type='submit'>File as Leave ({len(a['uncovered'])})</button></form> "
                       f"<form method='POST' action='/absence_action?ym={ym}' style='display:inline;'><input type='hidden' name='emp_id' value='{emp}'>"
                       f"<input type='hidden' name='action' value='absence'><button class='btn btn-danger' type='submit'>Unapproved Absence</button></form>")
        if a["uncovered"]:
            unc = (f"<span class='badge b-warn'>{len(a['uncovered'])}</span> "
                   + ", ".join(parse_date(d).strftime("%d %b") for d in a["uncovered"]))
        else:
            unc = "<span class='badge b-ok'>None</span>"
        return (f"<tr><td>{emp}</td><td><strong>{esc(a['name'])}</strong>{' <span class=note>(left)</span>' if left else ''}"
                f"<br><span class='note'>code {esc(s['attendance_code'] or '') if s else ''}</span></td>"
                f"<td class='num'>{a['days']}</td><td class='num'>{a['present']}</td><td class='num'>{a['late']}</td><td class='num'>{len(a['absent'])}</td>"
                f"<td class='num'>{len(a['covered'])}</td><td>{unc}</td><td>{actions}</td></tr>")

    rows = "".join(att_row(e, a) for e, a in sorted(summary.items()))
    cfg_att = get_settings_dict(conn)
    can_set_timing = role in ROLE_ACTIONS["/save_timing"]

    def timing_row(t):
        off = parse_off_days(t["off_days"])
        hours = (clock_minutes(t["end_time"]) - clock_minutes(t["start_time"])) / 60 * (7 - len(off))
        remove = ""
        if can_set_timing and not (role == ROLE_REVIEWER and t["emp_id"] == own_emp):
            remove = (f"<form method='POST' action='/delete_timing?ym={ym}'><input type='hidden' name='id' value='{t['id']}'>"
                      "<button class='btn btn-sec' type='submit'>Remove</button></form>")
        return (f"<tr><td>{t['emp_id']}</td><td><strong>{esc(t['employee_name'])}</strong></td><td>{t['date_from']}</td><td>{t['date_to'] or 'Until changed'}</td>"
                f"<td>{t['start_time']} to {t['end_time']}</td><td>{', '.join(WEEKDAY_NAMES[d] for d in sorted(off)) or 'None'}</td>"
                f"<td class='num'>{hours:g}</td><td>{esc(t['reason'] or '')}</td><td>{esc(t['set_by'] or '')}</td><td>{remove}</td></tr>")

    timing_list = conn.execute("""SELECT t.*, s.employee_name FROM staff_timings t JOIN staff s ON s.emp_id = t.emp_id
                                          ORDER BY t.emp_id, t.date_from DESC, t.id DESC""").fetchall()
    timing_opts = "".join(f"<option value='{e}'>{e} — {esc(s['employee_name'])}</option>" for e, s in sorted(staff_rows.items())
                          if s["status"] == "Active" and s["staff_type"] != EOBI_ONLY_TYPE and not (role == ROLE_REVIEWER and e == own_emp))
    off_boxes = "".join(f"<label style='flex-direction:row; align-items:center; gap:3px;'><input type='checkbox' name='off_{i}' value='1'{' checked' if i >= 5 else ''}> {d}</label>"
                        for i, d in enumerate(WEEKDAY_NAMES))
    timing_form = f"""
              <form class="inline" method="POST" action="/save_timing?ym={ym}">
                <label>Staff Member <select name="emp_id" required>{timing_opts}</select></label>
                <label>From <input type="date" name="date_from" required></label>
                <label>To (blank = until changed) <input type="date" name="date_to"></label>
                <label>Start Time <input type="time" name="start_time" required></label>
                <label>End Time <input type="time" name="end_time" required></label>
                <span><b>Days off:</b></span>{off_boxes}
                <label>Reason <input name="reason" required size="26" placeholder="e.g. Part-time, afternoon shift"></label>
                <button class="btn" type="submit">Save Timing</button>
              </form>""" if can_set_timing else ""
    timings_panel = f"""
            <div class="panel">
              <h3>Office Timings</h3>
              <p class="note">Everyone follows the firm's timing ({esc(cfg_att.get('office_start_time', '09:00'))} to {esc(cfg_att.get('office_end_time', '18:00'))}, Saturday and Sunday off) unless a timing is set here.
                A timing decides when the person is late and which days are off, so off days are not shown as absences. It does not change pay.
                {'The reviewer and the approver set timings; the reviewer cannot set his own.' if can_set_timing else 'Only the reviewer and the approver can set timings.'}</p>
              {timing_form}
              <table><thead><tr><th>ID</th><th>Name</th><th>From</th><th>To</th><th>Timing</th><th>Days Off</th><th class="num">Hours / Week</th><th>Reason</th><th>Set By</th><th></th></tr></thead>
                <tbody>{"".join(timing_row(t) for t in timing_list) or "<tr><td colspan='10'>No individual timings set.</td></tr>"}</tbody></table>
            </div>"""
    std_hours = float(cfg_att.get("standard_weekly_hours", 45))
    can_set_hours = role in ROLE_ACTIONS["/save_part_hours"] and not is_month_locked(conn, pm_str)
    hour_rows = ""
    for s in conn.execute("""SELECT * FROM staff WHERE status = 'Active' AND COALESCE(staff_type, '') != ?
                                     AND (weekly_hours IS NOT NULL OR emp_id IN (SELECT emp_id FROM part_time_hours)) ORDER BY emp_id""", (EOBI_ONLY_TYPE,)).fetchall():
        hours = weekly_hours_for(conn, s, pm_str)
        this_month = conn.execute("SELECT set_by, note FROM part_time_hours WHERE emp_id = ? AND payroll_month = ?", (s["emp_id"], pm_str)).fetchone()
        pay = round(s["monthly_salary"] * hours / std_hours, 0) if hours else s["monthly_salary"]
        state = (f"<span class='badge b-ok'>Updated by {esc(this_month['set_by'] or '')}</span>" if this_month
                 else f"<span class='badge b-warn'>Not updated for {pay_month_label(pm_str)}</span>")
        hour_rows += (f"<tr><td>{s['emp_id']}</td><td><strong>{esc(s['employee_name'])}</strong></td><td class='num'>{f'{hours:g}' if hours else 'Full time'}</td>"
                      f"<td class='num'>{s['monthly_salary']:,.0f}</td><td class='num'><strong>{pay:,.0f}</strong></td><td>{state}</td><td>{esc((this_month['note'] if this_month else '') or '')}</td></tr>")
    hours_opts = "".join(f"<option value='{e}'>{e} — {esc(s['employee_name'])}</option>" for e, s in sorted(staff_rows.items())
                         if s["status"] == "Active" and s["staff_type"] != EOBI_ONLY_TYPE and not (role == ROLE_REVIEWER and e == own_emp))
    hours_form = f"""
              <form class="inline" method="POST" action="/save_part_hours?ym={ym}">
                <label>Staff Member <select name="emp_id" required>{hours_opts}</select></label>
                <label>Hours per Week <input type="number" name="weekly_hours" step="0.5" min="0.5" required style="width:100px;" placeholder="{std_hours:g} = full time"></label>
                <label>Note <input name="note" size="26"></label>
                <button class="btn" type="submit">Save Hours for {pay_month_label(pm_str)}</button>
              </form>""" if can_set_hours else ""
    hours_panel = f"""
            <div class="panel">
              <h3>Part-Timer Hours — {pay_month_label(pm_str)}</h3>
              <p class="note">The reviewer updates each part-timer's weekly hours at the start of every month. Pay = full-time rate × hours ÷ {std_hours:g}.
                Hours carry forward until changed, and take priority over Weekly Hours on Staff Master.</p>
              {hours_form}
              <table><thead><tr><th>ID</th><th>Name</th><th class="num">Hours / Week</th><th class="num">Full-Time Rate</th><th class="num">Pay This Month</th><th>This Month</th><th>Note</th></tr></thead>
                <tbody>{hour_rows or "<tr><td colspan='7'>No part-timers.</td></tr>"}</tbody></table>
            </div>"""
    body = mobile_panel + f"""
            <div class="panel">
              <table><thead><tr><th>ID</th><th>Name</th><th class="num">Days</th><th class="num">Present</th><th class="num">Late</th>
                <th class="num">Absent</th><th class="num">Covered by Leave / Holiday</th><th>Absent — No Leave Filed</th><th></th></tr></thead>
                <tbody>{rows or "<tr><td colspan='9'>No attendance for this month yet.</td></tr>"}</tbody></table>
              <p class="note" style="margin-top:8px;">Late = arriving more than {esc(cfg_att.get('late_grace_minutes', '15'))} minutes after the person's start time. Late arrivals are never deducted.
                On {esc(cfg_att.get('late_hold_days', '3'))} late arrivals on working days in a calendar month (counted from {esc(cfg_att.get('late_hold_from', ''))}) that month's salary or stipend is held
                until the approver releases it on the Monthly Payroll tab.</p>
            </div>""" + hours_panel + timings_panel
    return body
