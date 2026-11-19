"""The payroll calculation engine."""

from datetime import date
from datetime import timedelta
import calendar

import config
from attendance.timing import late_arrival_days, late_days_text
from common.constants import ABSENCE_LEAVE_TYPE, ARTICLE_TYPES, EOBI_ONLY_TYPE, NOC_TYPE, NOT_DEDUCTED_TYPES
from common.dates import add_months, first_of_month, fmt_date, parse_date
from common.settings import get_settings_dict, is_month_locked
from database import get_conn
from staff.rules import cnic_alert, eobi_start_date
from timesheets.rules import timesheet_required

def annual_leave_bank(conn, staff_row, year, cfg):
    """Permanent / Intern leave bank. Each 1 January the year's leave count restarts; unused leave (24 + brought forward +
    approved credits - taken, never below 0) is carried forward up to `leave_bank_max` (40) and anything above it is paid
    in January's payroll. Returns (days brought forward into `year`, days encashed at the start of `year`)."""
    annual = float(cfg.get("leaves_per_year_permanent", 24))
    cap = float(cfg.get("leave_bank_max", 40))
    opening = parse_date(cfg.get("opening_balances_as_at", "2026-10-31"))
    emp_id = staff_row["emp_id"]
    carry = encash = 0.0
    # balances as at 31 Dec: the bank starts with the following year (nothing is known about the year before)
    first_year = (opening + timedelta(days=1)).year if opening else year
    for yr in range(first_year, year):
        taken = float(conn.execute("""SELECT COALESCE(SUM(no_of_days), 0) FROM leave_register WHERE emp_id = ?
                                      AND payroll_month BETWEEN ? AND ? AND status NOT IN ('Rejected', 'Cancelled')""",
                                   (emp_id, f"{yr}-01-01", f"{yr}-12-01")).fetchone()[0])
        if opening and yr == opening.year:
            taken += float(staff_row["opening_leaves_year"] or 0)
        credits = float(conn.execute("""SELECT COALESCE(SUM(days), 0) FROM leave_credits WHERE emp_id = ? AND status = 'Approved'
                                        AND work_date BETWEEN ? AND ?""", (emp_id, f"{yr}-01-01", f"{yr}-12-31")).fetchone()[0])
        unused = max(0.0, annual + carry + credits - taken)
        carry, encash = min(cap, unused), max(0.0, unused - cap)
    return carry, encash

def close_leavers(conn):
    """Staff whose leaving date has passed become 'Left' (until then they stay Active and are paid up to that date)."""
    conn.execute("UPDATE staff SET status = 'Left' WHERE status = 'Active' AND leaving_date IS NOT NULL AND leaving_date < ?",
                 (date.today().isoformat(),))

def weekly_hours_for(conn, staff, pm_str):
    """A part-timer's weekly hours for a payroll month: the latest monthly update up to that month, otherwise Staff Master. None = full time."""
    row = conn.execute("""SELECT weekly_hours FROM part_time_hours WHERE emp_id = ? AND payroll_month <= ?
                          ORDER BY payroll_month DESC LIMIT 1""", (staff["emp_id"], pm_str)).fetchone()
    return (row["weekly_hours"] if row else staff["weekly_hours"]) or None

def recalc_open_months():
    conn = get_conn()
    close_leavers(conn)
    conn.commit()
    months = [r["payroll_month"] for r in conn.execute("SELECT payroll_month FROM payroll_runs WHERE status = 'Draft'")]
    conn.close()
    for m in months:
        calculate_payroll(m)

def calculate_payroll(payroll_month_str):
    conn = get_conn()
    cfg = get_settings_dict(conn)

    pay_month = first_of_month(parse_date(payroll_month_str))
    pm_str = fmt_date(pay_month)
    if is_month_locked(conn, pm_str):
        conn.close()
        return

    days_in_month = calendar.monthrange(pay_month.year, pay_month.month)[1]
    month_end = date(pay_month.year, pay_month.month, days_in_month)

    eobi_wage = float(cfg.get("eobi_wage_base", 40700))
    eobi_er_rate = float(cfg.get("eobi_employer_share", 0.05))
    eobi_ee_rate = float(cfg.get("eobi_employee_share", 0.01))
    eobi_wait = int(float(cfg.get("eobi_permanent_wait_months", 3)))
    std_week_hours = float(cfg.get("standard_weekly_hours", 45))
    lv_monthly = float(cfg.get("paid_leaves_per_month", 2))
    lv_annual = float(cfg.get("leaves_per_year_permanent", 24))
    lv_icap = float(cfg.get("icap_articleship_leave_total", 130))
    intern_hold_months = int(float(cfg.get("intern_pay_hold_months", 4)))
    opening_date = parse_date(cfg.get("opening_balances_as_at", "2026-10-31"))

    conn.execute("INSERT OR IGNORE INTO payroll_runs (payroll_month, status) VALUES (?, 'Draft')", (pm_str,))
    banks = {r["bank_name"]: dict(r) for r in conn.execute("SELECT * FROM banks").fetchall()}
    conn.execute("DELETE FROM payroll_lines WHERE payroll_month = ?", (pm_str,))

    staff_rows = conn.execute("SELECT * FROM staff ORDER BY emp_id").fetchall()

    for s in staff_rows:
        emp_id = s["emp_id"]
        emp_name = s["employee_name"]
        stype = s["staff_type"] or ""
        doj = parse_date(s["date_of_joining"])
        dol = parse_date(s["leaving_date"])
        status = s["status"] or "Active"
        monthly_sal = float(s["monthly_salary"] or 0)
        if stype == "Intern" and config.INTERN_OLD_RATE and pm_str < config.INTERN_OLD_RATE[0]:
            monthly_sal = config.INTERN_OLD_RATE[1]  # the stipend in force before the revision
        month_hours = weekly_hours_for(conn, s, pm_str)
        if month_hours:  # part-timer: full-time rate pro-rated on this month's hours against the standard week
            monthly_sal = round(monthly_sal * float(month_hours) / std_week_hours, 0)
        opening_withheld = float(s["opening_withheld_probation"] or 0)
        icap_reg = parse_date(s["icap_reg_confirmed_date"])
        eobi_reg = parse_date(s["eobi_registration_date"])

        # 1. On payroll check
        on_payroll = True
        if doj and doj > month_end:
            on_payroll = False
        if dol and dol < pay_month:
            on_payroll = False
        if status == "Left" and not dol:
            on_payroll = False
        # EOBI-only people are covered for EOBI while active, but are never on the salary payroll
        eobi_only = stype == EOBI_ONLY_TYPE
        eobi_covered = on_payroll
        if eobi_only:
            on_payroll = False

        # 2. Days employed & Gross earned
        if not on_payroll:
            days_employed = 0
            gross_earned = 0.0
        else:
            start_d = max(pay_month, doj) if doj else pay_month
            end_d = min(month_end, dol) if dol else month_end
            days_employed = (end_d - start_d).days + 1
            gross_earned = round(monthly_sal / days_in_month * days_employed, 0)

        # 3. Pay hold at month end. Permanent staff: regular pay. ICAP / NOC students: stipend accumulates until the ICAP
        # registration card is received, no security. Internees: nothing paid until 4 months are complete, then all but
        # one month's pay is released; that month stays as security until the approver releases it at exit.
        hold_until = None
        if not on_payroll:
            on_probation = False
        elif stype in ARTICLE_TYPES:
            on_probation = icap_reg is None or icap_reg > month_end
        elif stype == "Intern":
            if doj:
                hold_months = s["probation_months"] if s["probation_months"] is not None else intern_hold_months
                hold_until = add_months(doj, int(hold_months)) - timedelta(days=1)
                on_probation = hold_until > month_end
            else:
                on_probation = opening_withheld > 0
        else:
            on_probation = False

        # 4. Additions
        ta = conn.execute("""
            SELECT COALESCE(SUM(total_amount), 0) FROM travel_claims
            WHERE emp_id = ? AND payroll_month = ? AND status = 'Reviewed'
        """, (emp_id, pm_str)).fetchone()[0]  # a claim is paid only once the reviewer has reviewed it

        mi = conn.execute("""
            SELECT * FROM monthly_inputs WHERE emp_id = ? AND payroll_month = ?
        """, (emp_id, pm_str)).fetchone()

        bonus = float(mi["bonus"]) if mi else 0.0
        arrears = float(mi["arrears_other"]) if mi else 0.0
        late_arr = 0.0  # firm policy: late arrivals are never deducted (repeated late arrivals hold the pay instead, see 7b-2)
        partial_client = float(mi["partial_days_client_deduction"]) if mi else 0.0
        lv_waived = float(mi["leave_days_waived"]) if mi else 0.0

        settlement = conn.execute("SELECT decision, amount, payroll_month FROM security_settlements WHERE emp_id = ?", (emp_id,)).fetchone()
        security_released = settlement["amount"] if settlement and settlement["decision"] == "Released" and settlement["payroll_month"] == pm_str else 0.0
        security_now = 0.0
        if not on_payroll or on_probation:
            prob_released = 0.0
        else:
            hist = conn.execute("""
                SELECT COALESCE(SUM(salary_withheld_probation), 0) AS w,
                       COALESCE(SUM(probation_salary_released), 0) AS r
                FROM payroll_lines
                WHERE emp_id = ? AND payroll_month < ?
            """, (emp_id, pm_str)).fetchone()
            outstanding = opening_withheld + float(hist["w"]) - float(hist["r"]) - (settlement["amount"] if settlement else 0.0)
            security_due = monthly_sal if stype == "Intern" and not settlement else 0.0
            prob_released = max(0.0, outstanding - security_due)
            security_now = max(0.0, outstanding - prob_released)  # pay already released is never taken back as security

        # 4b. Leave bank (Permanent / Intern): unused leave brought forward; days above the bank limit are paid in January
        carried_fwd = encash_days = leave_encash = 0.0
        if stype in ("Permanent", "Intern"):
            carried_fwd, encash_days = annual_leave_bank(conn, s, pay_month.year, cfg)
            if pay_month.month != 1 or not on_payroll:
                encash_days = 0.0
            leave_encash = round(monthly_sal / days_in_month * encash_days, 0)  # same daily rate as leave deductions

        total_additions = gross_earned + ta + bonus + arrears + prob_released + leave_encash

        # 5. Leaves & Excess Leave Deductions
        leaves_month = conn.execute("""
            SELECT COALESCE(SUM(no_of_days), 0) FROM leave_register
            WHERE emp_id = ? AND payroll_month = ? AND status NOT IN ('Rejected', 'Cancelled')
        """, (emp_id, pm_str)).fetchone()[0]

        # Study leave and urgent work outside Pakistan are never deducted: they still count towards leave balances,
        # but not towards the 2-per-month allowance
        study_month = conn.execute(f"""
            SELECT COALESCE(SUM(no_of_days), 0) FROM leave_register
            WHERE emp_id = ? AND payroll_month = ? AND leave_type IN ({','.join('?' * len(NOT_DEDUCTED_TYPES))})
              AND status NOT IN ('Rejected', 'Cancelled')
        """, (emp_id, pm_str, *NOT_DEDUCTED_TYPES)).fetchone()[0]

        excess_leaves = max(0.0, float(leaves_month) - float(study_month) - lv_monthly - lv_waived) if on_payroll else 0.0

        # 5b. Approved leave credits (gazetted holiday / weekend worked) cover days that would otherwise be deducted
        credits_to_date = conn.execute("""SELECT COALESCE(SUM(days), 0) FROM leave_credits
                                          WHERE emp_id = ? AND status = 'Approved' AND work_date <= ?""", (emp_id, fmt_date(month_end))).fetchone()[0]
        credits_used_before = conn.execute("SELECT COALESCE(SUM(comp_leave_used), 0) FROM payroll_lines WHERE emp_id = ? AND payroll_month < ?",
                                           (emp_id, pm_str)).fetchone()[0]
        comp_used = min(excess_leaves, max(0.0, float(credits_to_date) - float(credits_used_before)))
        excess_leaves -= comp_used
        leave_ded = round(min(gross_earned, (monthly_sal / days_in_month) * excess_leaves), 0) if on_payroll else 0.0

        # 6. Loan & Advance Recovery
        loans = conn.execute("SELECT * FROM advances_loans WHERE emp_id = ?", (emp_id,)).fetchall()
        loan_recovery = 0.0
        loan_notes = []
        for ln in loans:
            amt = float(ln["amount"])
            inst = amt if ln["loan_type"] == "Advance" else float(ln["monthly_instalment"] or 0)
            first_rec = parse_date(ln["first_recovery_month"])
            hold_prior = int(ln["months_on_hold_prior"] or 0)
            hold_now = int(ln["hold_this_month"] or 0)
            # money paid back directly by the end of this month reduces what is left to recover from salary
            repaid = float(conn.execute("SELECT COALESCE(SUM(amount), 0) FROM loan_repayments WHERE loan_ref = ? AND paid_on <= ?",
                                        (ln["loan_ref"], fmt_date(month_end))).fetchone()[0])

            if not first_rec or pay_month < first_rec or hold_now == 1:
                rec_this = 0.0
            else:
                months_elapsed = max(0, (pay_month.year - first_rec.year) * 12 + (pay_month.month - first_rec.month))
                recovered_before = min(amt, inst * max(0, months_elapsed - hold_prior))
                rec_this = min(inst, max(0.0, amt - recovered_before - repaid))
            loan_recovery += rec_this
            # a lump-sum loan is never deducted from salary; once its committed date passes unpaid, the payroll says so
            if ln["repayment_method"] == "Lump sum" and ln["lump_sum_due"] and ln["lump_sum_due"] <= fmt_date(month_end) and amt - repaid > 0:
                loan_notes.append(f"Loan {ln['loan_ref']}: lump sum of Rs. {amt - repaid:,.0f} was due on {ln['lump_sum_due']} and is not repaid.")

        # 7. Pay Held, Income Tax (Rs. 0), & Net Pay (loan instalments are still recovered from pay that is being held)
        payable_pay = max(0.0, gross_earned - leave_ded - late_arr - partial_client - loan_recovery)
        sal_withheld = payable_pay if on_probation else 0.0
        income_tax_ded = 0.0  # Firm policy: No income tax deducted right now
        net_pay = round(total_additions - leave_ded - late_arr - partial_client - loan_recovery - sal_withheld - income_tax_ded, 0)

        # 7b. Absence hold: a recorded unapproved absence withholds the whole month's net salary. It stays held until
        # the Approver releases it into a later payroll month; releases are paid even in a month with its own hold.
        absent_days = conn.execute("""
            SELECT COALESCE(SUM(no_of_days), 0) FROM leave_register
            WHERE emp_id = ? AND payroll_month = ? AND leave_type = ? AND status NOT IN ('Rejected', 'Cancelled')
        """, (emp_id, pm_str, ABSENCE_LEAVE_TYPE)).fetchone()[0]
        absence_released = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) FROM salary_releases WHERE emp_id = ? AND release_month = ?
        """, (emp_id, pm_str)).fetchone()[0]
        absence_hold = max(0.0, net_pay) if on_payroll and absent_days > 0 else 0.0
        net_pay = net_pay - absence_hold

        # 7b-2. Late-arrival hold: the set number of late arrivals in a calendar month holds that month's whole net salary / stipend
        # until the Approver releases it: into this month while it is Draft, otherwise into a later Draft month.
        late_days = late_arrival_days(conn, emp_id, pm_str, cfg) if on_payroll else []
        late_reached = len(late_days) >= int(float(cfg.get("late_hold_days", 3)))
        late_cleared = conn.execute("SELECT 1 FROM late_hold_releases WHERE emp_id = ? AND withheld_month = ?", (emp_id, pm_str)).fetchone() is not None
        late_hold = max(0.0, net_pay) if late_reached and not late_cleared else 0.0
        net_pay = net_pay - late_hold
        late_released = conn.execute("""SELECT COALESCE(SUM(amount), 0) FROM late_hold_releases
                                        WHERE emp_id = ? AND release_month = ? AND withheld_month < ?""", (emp_id, pm_str, pm_str)).fetchone()[0]

        # 7c. Timesheet hold: for ICAP students (and staff the reviewer has asked to submit timesheets) the whole net salary
        # is held until that month's timesheet is approved. Approval releases it: in the same month while it is still
        # Draft, otherwise automatically in the next Draft month.
        ts_hold, ts_released, ts_status = 0.0, 0.0, None
        if on_payroll and timesheet_required(s, pm_str, cfg):
            ts_row = conn.execute("SELECT status FROM timesheets WHERE emp_id = ? AND payroll_month = ?", (emp_id, pm_str)).fetchone()
            ts_status = ts_row["status"] if ts_row else "Not started"
            # if this month was sent back after its held salary was paid out in a later, signed-off month, keep holding it
            paid_elsewhere = conn.execute("""SELECT 1 FROM timesheet_releases tr JOIN payroll_runs r ON r.payroll_month = tr.release_month
                                             WHERE tr.emp_id = ? AND tr.withheld_month = ? AND r.status != 'Draft'""", (emp_id, pm_str)).fetchone()
            if ts_status != "Approved" or paid_elsewhere:
                ts_hold = max(0.0, net_pay)
        conn.execute("DELETE FROM timesheet_releases WHERE emp_id = ? AND release_month = ?", (emp_id, pm_str))
        for h in conn.execute("""
                SELECT p.payroll_month, p.salary_withheld_timesheet AS held FROM payroll_lines p
                JOIN payroll_runs r ON r.payroll_month = p.payroll_month
                JOIN timesheets t ON t.emp_id = p.emp_id AND t.payroll_month = p.payroll_month AND t.status = 'Approved'
                WHERE p.emp_id = ? AND p.payroll_month < ? AND r.status != 'Draft' AND p.salary_withheld_timesheet > 0
                  AND NOT EXISTS (SELECT 1 FROM timesheet_releases tr WHERE tr.emp_id = p.emp_id AND tr.withheld_month = p.payroll_month)
            """, (emp_id, pm_str)).fetchall():
            conn.execute("INSERT INTO timesheet_releases (emp_id, withheld_month, amount, release_month) VALUES (?,?,?,?)",
                         (emp_id, h["payroll_month"], h["held"], pm_str))
            ts_released += h["held"]
        net_pay = net_pay - ts_hold + absence_released + ts_released + security_released + late_released
        salary_expense = gross_earned + ta + bonus + arrears + leave_encash - leave_ded - late_arr - partial_client

        # 8. EOBI Calculations (Both 5% Employer + 1% Employee borne by Firm; Rs. 0 deducted from employee)
        # Enrolled staff: from the month of EOBI registration. Otherwise Permanent: from the month the waiting
        # period completes; ICAP Article: from the month of ICAP registration. Intern: never.
        # No arrears are accrued for the period before EOBI starts.
        eobi_from = eobi_start_date(stype, doj, icap_reg, eobi_reg, eobi_wait)
        if eobi_covered and eobi_from and eobi_from <= month_end:
            eobi_er = round(eobi_wage * eobi_er_rate, 0)
            eobi_ee = round(eobi_wage * eobi_ee_rate, 0)
        else:
            eobi_er = 0.0
            eobi_ee = 0.0
        eobi_accrued = eobi_er + eobi_ee
        eobi_to_deposit = eobi_accrued
        eobi_awaiting_icap = 0.0

        total_cost = salary_expense + eobi_accrued

        # 9. Leave Balances
        yr_start = fmt_date(date(pay_month.year, 1, 1))
        lv_ytd_reg = conn.execute("""
            SELECT COALESCE(SUM(no_of_days), 0) FROM leave_register
            WHERE emp_id = ? AND payroll_month >= ? AND payroll_month <= ?
              AND status NOT IN ('Rejected', 'Cancelled')
        """, (emp_id, yr_start, pm_str)).fetchone()[0]

        lv_cum_reg = conn.execute("""
            SELECT COALESCE(SUM(no_of_days), 0) FROM leave_register
            WHERE emp_id = ? AND payroll_month <= ?
              AND status NOT IN ('Rejected', 'Cancelled')
        """, (emp_id, pm_str)).fetchone()[0]

        open_yr = float(s["opening_leaves_year"] or 0) if (opening_date and pay_month.year == opening_date.year) else 0.0
        open_cum = float(s["opening_leaves_cumulative"] or 0)

        leaves_this_year = open_yr + float(lv_ytd_reg)
        leaves_cumulative = open_cum + float(lv_cum_reg)

        credits_year = conn.execute("""SELECT COALESCE(SUM(days), 0) FROM leave_credits WHERE emp_id = ? AND status = 'Approved'
                                       AND work_date >= ? AND work_date <= ?""", (emp_id, yr_start, fmt_date(month_end))).fetchone()[0]
        if stype in ("Permanent", "Intern"):
            leave_credit_days = float(credits_year)  # earned this calendar year
            leave_limit = lv_annual + carried_fwd + leave_credit_days
            leaves_counted = leaves_this_year
        elif stype == "ICAP Article" or (stype == NOC_TYPE and s["leave_entitlement_override"] is not None):
            leave_credit_days = float(credits_to_date)  # whole articleship (NOC: the residual leaves transferred with them)
            leave_limit = (float(s["leave_entitlement_override"]) if s["leave_entitlement_override"] is not None else lv_icap) + leave_credit_days
            leaves_counted = leaves_cumulative
        else:
            leave_credit_days = 0.0
            leave_limit = float(s["leave_entitlement_override"]) if s["leave_entitlement_override"] is not None else None
            leaves_counted = leaves_cumulative

        leave_balance = (leave_limit - leaves_counted) if leave_limit is not None else None
        if leave_balance is None:
            leave_flag = ""
        elif leave_balance < 0:
            leave_flag = "Over limit"
        elif leave_balance <= 5:
            leave_flag = "Low"
        else:
            leave_flag = "OK"

        # 10. Payment Mode & Checks
        b_info = banks.get(s["bank_name"] or "", {})
        is_meezan = (b_info.get("is_meezan") == 1) or (s["bank_name"] == "Meezan Bank")
        ibft_code = b_info.get("ibft_bank_code") or ""

        if eobi_only:
            pay_mode = "EOBI only"
        elif net_pay <= 0:
            pay_mode = "Nothing to pay"
        elif is_meezan:
            pay_mode = "Meezan IFT"
        else:
            pay_mode = "IBFT"

        checks = []
        if net_pay < 0:
            checks.append("Negative net pay.")
        if net_pay > 0 and not s["bank_account_iban"]:
            checks.append("No bank account.")
        if pay_mode == "IBFT" and not ibft_code:
            checks.append("IBFT bank code missing.")
        if stype == "Intern" and on_payroll and not doj:
            checks.append("Enter joining date (pay hold).")
        if on_probation and stype in ARTICLE_TYPES:
            checks.append("Stipend held: ICAP registration card not received.")
        elif on_probation and hold_until:
            checks.append(f"Pay held until 4 months complete ({hold_until.strftime('%d %b %Y')}).")
        if stype == "Intern" and on_payroll and not on_probation and not settlement and security_now > 0:
            checks.append(f"Security held: Rs. {security_now:,.0f}.")
        if stype == NOC_TYPE and s["leave_entitlement_override"] is None:
            checks.append("Enter residual leaves (NOC).")
        if security_released > 0:
            checks.append(f"Held pay / security released: Rs. {security_released:,.0f}.")
        if not stype:
            checks.append("Set staff type.")
        if stype == "Permanent" and not doj and not eobi_reg:
            checks.append("Enter joining date (EOBI).")
        if eobi_accrued > 0 and not eobi_reg:
            checks.append("EOBI registration pending.")
        if absence_hold > 0:
            checks.append(f"Salary withheld: unapproved absence ({float(absent_days):g} day(s)).")
        if late_hold > 0:
            checks.append(f"Salary held: {len(late_days)} late arrivals this month ({late_days_text(late_days)}); awaiting the approver's release.")
        elif late_reached and late_cleared:
            checks.append("Late-arrival hold released by the approver.")
        if late_released > 0:
            checks.append(f"Late-arrival hold released: Rs. {late_released:,.0f}.")
        if ts_hold > 0:
            checks.append(f"Salary held: timesheet {ts_status.lower()}.")
        cnic_warn = cnic_alert(s["cnic_expiry"], month_end)
        if on_payroll and cnic_warn.startswith("CNIC expired"):
            checks.append(cnic_warn + ".")
        checks.extend(loan_notes)
        if leave_encash > 0:
            checks.append(f"Leave encashment: {encash_days:g} unused day(s) above the {float(cfg.get('leave_bank_max', 40)):g}-day bank.")
        if eobi_only:  # no salary, bank account or leave to check
            checks = [] if eobi_reg else ["Enter EOBI registration date."]

        conn.execute("""
            INSERT INTO payroll_lines (
                payroll_month, emp_id, employee_name, staff_type, on_payroll,
                monthly_salary, days_in_month, days_employed, gross_earned, on_probation,
                travelling_allowance, bonus, arrears_other, probation_salary_released, total_additions,
                leaves_this_month, leave_days_waived, excess_leave_days, leave_deduction,
                late_arrival, partial_days_client, loan_advance_recovery, salary_withheld_probation,
                salary_withheld_absence, absence_salary_released, salary_withheld_timesheet, timesheet_salary_released,
                salary_withheld_late, late_salary_released,
                leave_credit_days, comp_leave_used, leave_carried_forward, leave_encash_days, leave_encashment, security_released, income_tax_deduction, net_pay, salary_expense, eobi_employer_5pct, eobi_employee_1pct_borne_by_firm,
                eobi_cost_accrued, eobi_to_deposit, eobi_awaiting_icap, total_cost_to_firm,
                leaves_this_year, leaves_cumulative, leave_limit, leave_balance, leave_flag,
                payment_mode, validation_checks
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            pm_str, emp_id, emp_name, stype, 1 if on_payroll else 0,
            monthly_sal, days_in_month, days_employed, gross_earned, 1 if on_probation else 0,
            ta, bonus, arrears, prob_released, total_additions,
            leaves_month, lv_waived, excess_leaves, leave_ded,
            late_arr, partial_client, loan_recovery, sal_withheld,
            absence_hold, absence_released, ts_hold, ts_released,
            late_hold, late_released,
            leave_credit_days, comp_used, carried_fwd, encash_days, leave_encash, security_released, income_tax_ded, net_pay, salary_expense, eobi_er, eobi_ee,
            eobi_accrued, eobi_to_deposit, eobi_awaiting_icap, total_cost,
            leaves_this_year, leaves_cumulative, leave_limit, leave_balance, leave_flag,
            pay_mode, " ".join(checks)
        ))

    conn.commit()
    conn.close()

def effective_salary(conn, staff_row, pm_str, cfg):
    """Monthly salary actually paid in a month (part-timers pro-rated on that month's hours against the 45-hour week)."""
    sal = float(staff_row["monthly_salary"] or 0)
    hours = weekly_hours_for(conn, staff_row, pm_str)
    if hours:
        sal = round(sal * float(hours) / float(cfg.get("standard_weekly_hours", 45)), 0)
    return sal

def held_balance(conn, staff_row, pm_str):
    """Pay still held for a person up to and including this payroll month (held pay or security), less any settlement."""
    h = conn.execute("""SELECT COALESCE(SUM(salary_withheld_probation), 0) w, COALESCE(SUM(probation_salary_released), 0) r
                        FROM payroll_lines WHERE emp_id = ? AND payroll_month <= ?""", (staff_row["emp_id"], pm_str)).fetchone()
    settled = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM security_settlements WHERE emp_id = ?", (staff_row["emp_id"],)).fetchone()[0]
    return float(staff_row["opening_withheld_probation"] or 0) + float(h["w"]) - float(h["r"]) - float(settled)
