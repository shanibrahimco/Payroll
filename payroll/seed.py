"""One-time set-up from the Excel workbook."""

import os

import config
from common.dates import add_months, first_of_month, fmt_date, get_payroll_month_for_event, parse_date
from config import COMPANIES
from database import get_conn

def seed_from_excel(xlsx_path=None):
    xlsx_path = xlsx_path or config.EXCEL_SOURCE
    preparer, reviewer, approver = COMPANIES[config.COMPANY_KEY]["officers"]
    conn = get_conn()
    default_settings = [
        ("firm_name", config.FIRM_NAME, "Firm legal name"),
        ("cutoff_day", "25", "Leave / travel cut-off day of month"),
        ("travel_rate_per_km", "17", "Travel rate per km used only when no petrol price is on file (Rs.)"),
        ("travel_rate_base_km", "17", "Formula: Rs. per km paid when the petrol price equals travel_rate_base_price"),
        ("travel_rate_base_price", "400", "Formula: petrol price (Rs./litre) at which travel_rate_base_km applies"),
        ("fuel_price_url", "https://psopk.com/fuel-prices/pol/archives?page=1", "PSO price archive the Premier Euro 5 price is read from"),
        ("travel_rate_max_uplift_pct", "10", "Most the reviewer may raise the formula travel rate (%), with a written reason"),
        ("deduct_income_tax", "0", "0 = No income tax deducted right now"),
        ("eobi_wage_base", "40700", "EOBI minimum wage base (Rs.)"),
        ("eobi_employer_share", "0.05", "EOBI employer share (5%)"),
        ("eobi_employee_share", "0.01", "EOBI employee share borne by employer (1%)"),
        ("eobi_permanent_wait_months", "3", "Months after joining before EOBI starts for permanent staff"),
        ("standard_weekly_hours", "45", "Full-time hours per week (9 hours x 5 days); part-timers paid pro-rata"),
        ("paid_leaves_per_month", "2", "Monthly paid leave allowance"),
        ("leaves_per_year_permanent", "24", "Annual leave entitlement for permanent staff"),
        ("leave_bank_max", "40", "Permanent / Intern: most unused leave carried into the next year; days above it are paid in January"),
        ("loan_max_salaries", "2", "Loans / advances outstanding may not exceed this many monthly salaries without an approved exception"),
        ("loan_max_instalments", "20", "Most monthly instalments a loan can be repaid over"),
        ("icap_articleship_leave_total", "130", "Total leave entitlement over 3.5-year ICAP articleship"),
        ("intern_pay_hold_months", "4", "Internees: months from joining before pay is released (one month is then kept as security)"),
        ("credit_apply_days", "7", "Days after the date worked within which a credit leave application must be submitted; later ones need the reviewer's condonation"),
        ("late_hold_days", "3", "Late arrivals in one calendar month at which the month's salary / stipend is held until the Approver releases it"),
        ("late_hold_from", "2026-11-26", "Late arrivals count towards the salary hold from this date"),
        ("office_start_time", "09:00", "Office start time"),
        ("office_end_time", "18:00", "Office closing time"),
        ("late_grace_minutes", "15", "An arrival more than this many minutes after the office start time is late"),
        ("timesheet_required_from", "2026-11-01", "ICAP students' salary is held until that month's timesheet is approved, from this month"),
        ("timesheet_online_from", "2026-12-01", "From this month timesheets are filled in day by day here; earlier months are uploaded scans"),
        ("timesheet_hours_per_day", "8", "Working hours per day on timesheets (9-hour office day less 1-hour lunch break)"),
        ("app_url", f"http://127.0.0.1:{config.PORT}", "Payroll address staff open in their browser (put in login emails); change when deployed"),
        ("opening_balances_as_at", "2026-10-31", "Opening balances cut-off date"),
        ("preparer", preparer, "Default Payroll Preparer"),
        ("reviewer", reviewer, "Default Payroll Reviewer"),
        ("approver", approver, "Default Payroll Approver")
    ]
    for k, v, desc in default_settings:
        conn.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (k, v, desc))

    # The workbook only seeds a fresh database. Once staff exist, every pay change goes through approval,
    # so re-importing (which overwrites staff, leaves and loans) must never happen on a later start.
    has_staff = conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0] > 0
    if has_staff or not xlsx_path or not os.path.exists(xlsx_path):
        conn.commit()
        conn.close()
        return

    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)

    ws_set = wb["Settings"]
    for r in range(6, 16):
        b_name = ws_set.cell(r, 11).value
        iban_cd = ws_set.cell(r, 12).value
        ibft_cd = ws_set.cell(r, 13).value
        if b_name:
            conn.execute("""
                INSERT OR REPLACE INTO banks (bank_name, iban_code, ibft_bank_code, is_meezan)
                VALUES (?, ?, ?, ?)
            """, (str(b_name).strip(), str(iban_cd or "").strip(), str(ibft_cd or "").strip(), 1 if "Meezan" in str(b_name) else 0))

    ws_st = wb["Staff"]
    for r in range(6, 66):
        emp_id = ws_st.cell(r, 1).value
        if not emp_id:
            continue
        conn.execute("""
            INSERT OR REPLACE INTO staff (
                emp_id, employee_name, cnic, designation, staff_type, date_of_joining,
                probation_months, articleship_end_date, leave_entitlement_override,
                opening_leaves_cumulative, opening_leaves_year, monthly_salary,
                bank_account_iban, bank_name, account_title, status, leaving_date,
                opening_withheld_probation, icap_reg_confirmed_date, remarks
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            str(emp_id).strip(),
            ws_st.cell(r, 2).value,
            ws_st.cell(r, 3).value,
            ws_st.cell(r, 4).value,
            ws_st.cell(r, 5).value,
            fmt_date(parse_date(ws_st.cell(r, 7).value)),
            ws_st.cell(r, 8).value,
            fmt_date(parse_date(ws_st.cell(r, 10).value)),
            ws_st.cell(r, 11).value,
            float(ws_st.cell(r, 13).value or 0),
            float(ws_st.cell(r, 14).value or 0),
            float(ws_st.cell(r, 15).value or 0),
            str(ws_st.cell(r, 16).value or "").strip(),
            ws_st.cell(r, 17).value,
            ws_st.cell(r, 21).value,
            ws_st.cell(r, 22).value or "Active",
            fmt_date(parse_date(ws_st.cell(r, 23).value)),
            float(ws_st.cell(r, 24).value or 0),
            fmt_date(parse_date(ws_st.cell(r, 26).value)),
            ws_st.cell(r, 25).value
        ))

    ws_lv = wb["Leave Register"]
    conn.execute("DELETE FROM leave_register")
    for r in range(6, ws_lv.max_row + 1):
        emp_id = ws_lv.cell(r, 2).value
        if not emp_id:
            continue
        app_d = parse_date(ws_lv.cell(r, 1).value)
        f_d = parse_date(ws_lv.cell(r, 5).value)
        t_d = parse_date(ws_lv.cell(r, 6).value)
        pm_d = parse_date(ws_lv.cell(r, 12).value) or get_payroll_month_for_event(f_d)
        conn.execute("""
            INSERT INTO leave_register (
                application_date, emp_id, leave_type, date_from, date_to,
                no_of_days, reason, recommended_by, approved_by, status, payroll_month, remarks
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            fmt_date(app_d), str(emp_id).strip(), ws_lv.cell(r, 4).value or "Casual",
            fmt_date(f_d), fmt_date(t_d), float(ws_lv.cell(r, 7).value or 0),
            ws_lv.cell(r, 8).value, ws_lv.cell(r, 9).value, ws_lv.cell(r, 10).value,
            ws_lv.cell(r, 11).value or "Pending", fmt_date(pm_d), ws_lv.cell(r, 13).value
        ))

    ws_al = wb["Advances & Loans"]
    for r in range(6, ws_al.max_row + 1):
        ref = ws_al.cell(r, 1).value
        if not ref:
            continue
        dt_given = parse_date(ws_al.cell(r, 5).value)
        first_m = parse_date(ws_al.cell(r, 8).value) or add_months(first_of_month(dt_given), 1)
        conn.execute("""
            INSERT OR REPLACE INTO advances_loans (
                loan_ref, emp_id, loan_type, date_given, amount, monthly_instalment,
                first_recovery_month, months_on_hold_prior, hold_this_month, purpose,
                no_of_instalments, recommended_by, approved_by, approval_date, remarks
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            str(ref).strip(), str(ws_al.cell(r, 2).value).strip(),
            ws_al.cell(r, 4).value or "Loan", fmt_date(dt_given),
            float(ws_al.cell(r, 6).value or 0), float(ws_al.cell(r, 7).value or 0),
            fmt_date(first_m), int(ws_al.cell(r, 9).value or 0),
            1 if str(ws_al.cell(r, 10).value or "").upper() == "Y" else 0,
            ws_al.cell(r, 19).value, ws_al.cell(r, 20).value,
            ws_al.cell(r, 22).value, ws_al.cell(r, 17).value,
            fmt_date(parse_date(ws_al.cell(r, 23).value)), ws_al.cell(r, 18).value
        ))

    conn.commit()
    conn.close()
