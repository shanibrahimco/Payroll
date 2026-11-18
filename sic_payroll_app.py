#!/usr/bin/env python3
"""
Shan Ibrahim & Co. Chartered Accountants - Payroll Management System
Backend: Python 3 + SQLite (Built-in standard library + optional openpyxl)
Rules configured:
- Income Tax Deduction: Disabled (Rs. 0 deducted)
- EOBI Contribution: 5% Employer + 1% Employee BOTH borne by Employer (Rs. 2,442/mo on Rs. 40,700 base; Rs. 0 deducted from employee)
- EOBI Eligibility: Permanent staff 3 months after joining; ICAP Articles from ICAP registration date; Interns never
- Travelling Allowance Rate: Revised to Rs. 17 per km (+ Parking + Other)
- Cut-off Date: 25th of each month for Leaves & Travel Claims
"""

import argparse
import sqlite3
import os
import csv
import calendar
import hashlib
import hmac
import html as html_lib
import json
import secrets
from datetime import datetime, date, timedelta
from http.cookies import SimpleCookie
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Each company has its own database, exports folder, users and port, so one firm never sees another's payroll.
# Choose with:  python sic_payroll_app.py --company elite
COMPANIES = {
    "sic": {
        "name": "Shan Ibrahim & Co. Chartered Accountants",
        "short": "Shan Ibrahim & Co.",
        "db": os.path.join(BASE_DIR, "sic_payroll.db"),
        "exports": os.path.join(BASE_DIR, "exports"),
        "uploads": os.path.join(BASE_DIR, "uploads"),
        "excel": os.path.join(BASE_DIR, "Payroll Workbook - Shan Ibrahim & Co.xlsx"),
        "port": 8080,
        "officers": ("", "", ""),  # preparer, reviewer, approver: names are kept in local_settings.json
        # Where ICAP records the students' training (the first is the default). Payroll stays with this firm either way.
        "training_orgs": ("Alam & Aulakh Chartered Accountants", "Shan Ibrahim & Co. Chartered Accountants"),
        "intern_old_rate": ("2026-11-01", 15000.0),  # internees' full-time stipend before this date (Rs. 22,500 from it)
    },
    "elite": {
        "name": "Elite Financial Consultants Cost & Management Accountants",
        "short": "Elite Financial Consultants",
        "db": os.path.join(BASE_DIR, "companies", "elite", "payroll.db"),
        "exports": os.path.join(BASE_DIR, "companies", "elite", "exports"),
        "uploads": os.path.join(BASE_DIR, "companies", "elite", "uploads"),
        "excel": None,  # starts empty: staff are added through the app
        "port": 8081,
        "officers": ("", "", ""),
    },
}
DEFAULT_COMPANY = "sic"

# People's names stay in this file on the computer running the app; it is never uploaded with the code.
LOCAL_SETTINGS_PATH = os.path.join(BASE_DIR, "local_settings.json")
if os.path.exists(LOCAL_SETTINGS_PATH):
    with open(LOCAL_SETTINGS_PATH, encoding="utf-8") as local_settings_file:
        for company_key, local_values in json.load(local_settings_file).items():
            if company_key in COMPANIES and "officers" in local_values:
                COMPANIES[company_key]["officers"] = tuple(local_values["officers"])

def use_company(key):
    """Point the app at one company's database and branding."""
    global COMPANY_KEY, FIRM_NAME, FIRM_SHORT, DB_PATH, EXPORT_DIR, UPLOAD_DIR, EXCEL_SOURCE, PORT, TRAINING_ORGS, INTERN_OLD_RATE
    c = COMPANIES[key]
    INTERN_OLD_RATE = c.get("intern_old_rate")
    TRAINING_ORGS = c.get("training_orgs") or (c["name"],)
    COMPANY_KEY, FIRM_NAME, FIRM_SHORT = key, c["name"], c["short"]
    DB_PATH, EXPORT_DIR, UPLOAD_DIR, EXCEL_SOURCE, PORT = c["db"], c["exports"], c["uploads"], c["excel"], c["port"]
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

use_company(DEFAULT_COMPANY)

# ==============================================================================
# 1. DATABASE SCHEMA & INITIALIZATION
# ==============================================================================

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS banks (
    bank_name TEXT PRIMARY KEY,
    iban_code TEXT NOT NULL,
    ibft_bank_code TEXT,
    is_meezan INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS staff (
    emp_id TEXT PRIMARY KEY,
    employee_name TEXT NOT NULL,
    cnic TEXT,
    designation TEXT,
    staff_type TEXT, -- Permanent, ICAP Article, NOC Article, Intern
    date_of_joining TEXT, -- YYYY-MM-DD
    probation_months INTEGER,
    articleship_end_date TEXT,
    leave_entitlement_override REAL,
    opening_leaves_cumulative REAL DEFAULT 0,
    opening_leaves_year REAL DEFAULT 0,
    monthly_salary REAL NOT NULL DEFAULT 0,
    bank_account_iban TEXT,
    bank_name TEXT REFERENCES banks(bank_name),
    account_title TEXT,
    status TEXT DEFAULT 'Active', -- Active, Left
    leaving_date TEXT,
    opening_withheld_probation REAL DEFAULT 0,
    icap_reg_confirmed_date TEXT,
    remarks TEXT,
    eobi_registration_date TEXT, -- date actually enrolled with EOBI
    weekly_hours REAL, -- part-timers only; NULL = full time (standard_weekly_hours)
    eobi_no TEXT,
    date_of_birth TEXT,
    icap_crn TEXT, -- ICAP student registration no., printed on the monthly timesheet
    timesheet_from TEXT, -- non-ICAP staff the reviewer asked to submit timesheets: first month required (NULL = not required)
    email TEXT,
    cnic_issue_date TEXT,
    cnic_expiry TEXT, -- the system warns when the CNIC has expired or expires soon
    attendance_code TEXT -- EmpCode in the attendance machine software
);

CREATE TABLE IF NOT EXISTS attendance_uploads ( -- attendance report from the attendance machine software (starts the payroll)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payroll_month TEXT NOT NULL,
    file_name TEXT,
    stored_file TEXT,
    period_from TEXT,
    period_to TEXT,
    uploaded_by TEXT,
    uploaded_at TEXT,
    parsed TEXT, -- JSON: [{code, name, days: {date: status}, summary}]
    unmatched TEXT -- JSON: [code / name] not matched to staff
);

CREATE TABLE IF NOT EXISTS attendance (
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    att_date TEXT NOT NULL,
    status TEXT NOT NULL, -- as in the report: P, P-LT (late), A (absent), WO (weekly off), HL, L ...
    upload_id INTEGER,
    PRIMARY KEY (emp_id, att_date)
);

CREATE TABLE IF NOT EXISTS security_settlements ( -- held pay / security of a leaver, settled once by the approver
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_id TEXT NOT NULL UNIQUE REFERENCES staff(emp_id),
    decision TEXT NOT NULL, -- Released (paid in payroll_month) or Forfeited
    amount REAL NOT NULL,
    payroll_month TEXT NOT NULL,
    note TEXT,
    decided_by TEXT,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS leave_imports ( -- leave history (opening balances + day-by-day leaves) uploaded by the approver
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT,
    uploaded_by TEXT,
    uploaded_at TEXT,
    status TEXT NOT NULL DEFAULT 'Preview', -- Preview, Applied, Discarded
    data TEXT, -- JSON {opening: {emp: days}, leaves: [...], problems: [...]}
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS payroll_imports ( -- a final payroll sheet (Excel) uploaded by the approver for one month
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payroll_month TEXT NOT NULL,
    file_name TEXT,
    stored_file TEXT,
    sheet_name TEXT,
    uploaded_by TEXT,
    uploaded_at TEXT,
    status TEXT NOT NULL DEFAULT 'Preview', -- Preview, Applied, Discarded
    mapping TEXT, -- JSON: sheet column heading -> payroll field
    rows TEXT, -- JSON: matched rows {emp_id: {field: value}}
    unmatched TEXT, -- JSON: sheet rows that matched no staff member
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS fuel_prices ( -- PSO Premier Euro 5 petrol price; sets the travel rate per km
    effective_date TEXT PRIMARY KEY,
    price REAL NOT NULL, -- Rs. per litre
    source TEXT NOT NULL, -- 'PSO website' or 'Manual'
    recorded_by TEXT,
    recorded_at TEXT
);

CREATE TABLE IF NOT EXISTS rate_uplifts ( -- increase on top of the formula travel rate (reviewer: at most 10%, with a written reason)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_from TEXT NOT NULL, -- claims dated on / after this use it
    pct REAL NOT NULL, -- 0 = back to the plain formula rate
    reason TEXT NOT NULL,
    set_by TEXT,
    set_at TEXT
);

CREATE TABLE IF NOT EXISTS doc_types ( -- documents kept for each staff member; the approver decides which are mandatory
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    applies_to TEXT NOT NULL DEFAULT 'All', -- All, or one staff type (Permanent, ICAP Article, Intern)
    mandatory INTEGER NOT NULL DEFAULT 1, -- must be attached when a new staff member is added
    has_expiry INTEGER NOT NULL DEFAULT 0, -- ask for the document's expiry date (CNIC)
    active INTEGER NOT NULL DEFAULT 1,
    added_by TEXT,
    added_at TEXT
);

CREATE TABLE IF NOT EXISTS staff_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_id TEXT NOT NULL, -- no foreign key: a new person's documents arrive with the add-staff request, before approval
    doc_type TEXT NOT NULL,
    file TEXT NOT NULL, -- stored name in the uploads/documents folder
    original_name TEXT,
    mime TEXT,
    expiry_date TEXT,
    uploaded_by TEXT,
    uploaded_at TEXT,
    change_request_id INTEGER, -- the add-staff request it came with (deleted if that request is rejected)
    replaced INTEGER NOT NULL DEFAULT 0 -- a newer copy of the same document was uploaded
);

CREATE TABLE IF NOT EXISTS leave_register (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_date TEXT NOT NULL,
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    leave_type TEXT NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    no_of_days REAL NOT NULL,
    reason TEXT,
    recommended_by TEXT,
    approved_by TEXT,
    status TEXT DEFAULT 'Pending',
    payroll_month TEXT NOT NULL,
    remarks TEXT,
    entered_by TEXT -- who filed it (e.g. '<operator> for <staff name>' when operating a delegated portal)
);

CREATE TABLE IF NOT EXISTS travel_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_date TEXT NOT NULL,
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    venue_particular TEXT,
    client_name TEXT,
    no_of_visits INTEGER DEFAULT 1,
    kms REAL DEFAULT 0,
    rate_per_km REAL DEFAULT 17,
    parking REAL DEFAULT 0,
    other_charges REAL DEFAULT 0,
    total_amount REAL NOT NULL,
    payroll_month TEXT NOT NULL,
    remarks TEXT,
    status TEXT DEFAULT 'Submitted', -- Submitted, Reviewed (paid in payroll), Rejected
    chargeable INTEGER DEFAULT 0, -- 1 = out-of-pocket expense chargeable to the client (set by the reviewer)
    submitted_by TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    invoiced INTEGER DEFAULT 0, -- chargeable claim included in a client invoice
    invoice_no TEXT,
    invoiced_by TEXT,
    invoiced_at TEXT,
    travel_mode TEXT DEFAULT 'Own vehicle', -- Own vehicle (kms x rate) or a ride service paid at the actual fare
    fare REAL DEFAULT 0, -- actual fare of a ride service (InDrive, Yango, cab, auto, bike)
    receipt_file TEXT, -- optional receipt / screenshot (uploads/receipts)
    receipt_name TEXT,
    receipt_type TEXT
);

CREATE TABLE IF NOT EXISTS monthly_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payroll_month TEXT NOT NULL,
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    bonus REAL DEFAULT 0,
    arrears_other REAL DEFAULT 0,
    late_arrival_deduction REAL DEFAULT 0,
    partial_days_client_deduction REAL DEFAULT 0,
    leave_days_waived REAL DEFAULT 0,
    note TEXT,
    UNIQUE(payroll_month, emp_id)
);

CREATE TABLE IF NOT EXISTS advances_loans (
    loan_ref TEXT PRIMARY KEY,
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    loan_type TEXT NOT NULL,
    date_given TEXT NOT NULL,
    amount REAL NOT NULL,
    monthly_instalment REAL DEFAULT 0,
    first_recovery_month TEXT NOT NULL,
    months_on_hold_prior INTEGER DEFAULT 0,
    hold_this_month INTEGER DEFAULT 0,
    purpose TEXT,
    no_of_instalments INTEGER,
    recommended_by TEXT,
    approved_by TEXT,
    approval_date TEXT,
    remarks TEXT,
    disbursed INTEGER DEFAULT 1, -- 0 = approved online, still to be paid by a separate loan IBFT / IFT file
    disbursed_at TEXT,
    disbursed_by TEXT,
    disbursement_batch TEXT -- loan bank-file batch it was paid in
);

CREATE TABLE IF NOT EXISTS payroll_runs (
    payroll_month TEXT PRIMARY KEY,
    status TEXT DEFAULT 'Draft',
    prepared_by TEXT,
    prepared_date TEXT,
    reviewed_by TEXT,
    reviewed_date TEXT,
    approved_by TEXT,
    approved_date TEXT,
    imported_from TEXT -- final payroll sheet (Excel) the month's figures came from
);

CREATE TABLE IF NOT EXISTS payroll_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payroll_month TEXT NOT NULL REFERENCES payroll_runs(payroll_month),
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    employee_name TEXT,
    staff_type TEXT,
    on_payroll INTEGER DEFAULT 1,
    monthly_salary REAL DEFAULT 0,
    days_in_month INTEGER DEFAULT 30,
    days_employed INTEGER DEFAULT 30,
    gross_earned REAL DEFAULT 0,
    on_probation INTEGER DEFAULT 0,
    travelling_allowance REAL DEFAULT 0,
    bonus REAL DEFAULT 0,
    arrears_other REAL DEFAULT 0,
    probation_salary_released REAL DEFAULT 0,
    total_additions REAL DEFAULT 0,
    leaves_this_month REAL DEFAULT 0,
    leave_days_waived REAL DEFAULT 0,
    excess_leave_days REAL DEFAULT 0,
    leave_deduction REAL DEFAULT 0,
    late_arrival REAL DEFAULT 0,
    partial_days_client REAL DEFAULT 0,
    loan_advance_recovery REAL DEFAULT 0,
    salary_withheld_probation REAL DEFAULT 0,
    salary_withheld_absence REAL DEFAULT 0, -- whole net salary held for an unapproved absence
    absence_salary_released REAL DEFAULT 0, -- earlier absence holds released by the Approver into this month
    security_released REAL DEFAULT 0, -- held pay / security paid to a leaver on the approver's release
    leave_carried_forward REAL DEFAULT 0, -- Permanent / Intern: unused leave brought forward from last year (bank, max leave_bank_max)
    leave_encash_days REAL DEFAULT 0, -- unused leave above the bank limit, paid in January
    leave_encashment REAL DEFAULT 0, -- amount paid for those days
    leave_credit_days REAL DEFAULT 0, -- approved leave credits added to the entitlement (holiday / weekend worked)
    comp_leave_used REAL DEFAULT 0, -- leave credits used this month to cover days that would otherwise be deducted
    salary_withheld_timesheet REAL DEFAULT 0, -- ICAP students: whole net salary held until the month's timesheet is approved
    timesheet_salary_released REAL DEFAULT 0, -- earlier timesheet holds paid out in this month once approved
    income_tax_deduction REAL DEFAULT 0,
    net_pay REAL DEFAULT 0,
    salary_expense REAL DEFAULT 0,
    eobi_employer_5pct REAL DEFAULT 0,
    eobi_employee_1pct_borne_by_firm REAL DEFAULT 0,
    eobi_cost_accrued REAL DEFAULT 0,
    eobi_to_deposit REAL DEFAULT 0,
    eobi_awaiting_icap REAL DEFAULT 0,
    total_cost_to_firm REAL DEFAULT 0,
    leaves_this_year REAL DEFAULT 0,
    leaves_cumulative REAL DEFAULT 0,
    leave_limit REAL,
    leave_balance REAL,
    leave_flag TEXT,
    payment_mode TEXT,
    validation_checks TEXT,
    UNIQUE(payroll_month, emp_id)
);

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'Preparer', -- Approver, Reviewer, Preparer, Staff
    emp_id TEXT, -- the staff member this login belongs to (required for Staff)
    must_change_pw INTEGER DEFAULT 0 -- 1 = password was set by the approver; must be changed at next login
);

CREATE TABLE IF NOT EXISTS part_time_hours ( -- a part-timer's weekly hours, updated by the reviewer each month; they carry forward until changed
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    payroll_month TEXT NOT NULL, -- YYYY-MM-01
    weekly_hours REAL, -- NULL = full time
    note TEXT,
    set_by TEXT,
    set_at TEXT,
    PRIMARY KEY (emp_id, payroll_month)
);

CREATE TABLE IF NOT EXISTS staff_timings ( -- a person's own office timing and off days (part-timers, different shifts); others follow the firm's timing
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    date_from TEXT NOT NULL,
    date_to TEXT, -- blank = continues until replaced
    start_time TEXT NOT NULL, -- HH:MM
    end_time TEXT NOT NULL,
    off_days TEXT NOT NULL DEFAULT '5,6', -- weekdays off: 0 = Monday ... 6 = Sunday
    reason TEXT,
    set_by TEXT,
    set_at TEXT
);

CREATE TABLE IF NOT EXISTS late_hold_releases ( -- the Approver's release of pay held for late arrivals
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    withheld_month TEXT NOT NULL, -- payroll month the pay was held in
    amount REAL NOT NULL DEFAULT 0,
    release_month TEXT NOT NULL, -- payroll month it is paid in (the same month while that month is still Draft)
    note TEXT,
    released_by TEXT,
    released_at TEXT,
    UNIQUE (emp_id, withheld_month)
);

CREATE TABLE IF NOT EXISTS salary_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    withheld_month TEXT NOT NULL, -- approved payroll month the salary was withheld in
    amount REAL NOT NULL,
    release_month TEXT NOT NULL, -- payroll month it is paid out in
    released_by TEXT NOT NULL,
    released_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, -- add_staff, update_staff, add_input, add_loan
    emp_id TEXT,
    payroll_month TEXT,
    payload TEXT NOT NULL, -- JSON, applied as-is on approval
    summary TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending', -- Pending, Approved, Rejected
    decided_by TEXT,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL REFERENCES users(username),
    expires_at TEXT NOT NULL, -- ISO datetime
    acting_for TEXT -- emp_id whose portal this user is currently operating (portal_delegates)
);

CREATE TABLE IF NOT EXISTS portal_delegates ( -- staff who can't use the portal themselves; their portal is operated by another user
    emp_id TEXT PRIMARY KEY REFERENCES staff(emp_id),
    delegate_username TEXT NOT NULL REFERENCES users(username),
    granted_by TEXT,
    granted_at TEXT
);

CREATE TABLE IF NOT EXISTS timesheets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    payroll_month TEXT NOT NULL, -- calendar month the diary covers (YYYY-MM-01)
    mode TEXT NOT NULL, -- 'Scanned' (paper sheet uploaded, totals typed in) or 'Daily' (filled in day by day here)
    status TEXT NOT NULL DEFAULT 'Draft', -- Draft, Submitted, Reviewed, Approved, Returned
    total_hours REAL DEFAULT 0,
    standard_hours REAL DEFAULT 0,
    leave_days REAL DEFAULT 0,
    study_days REAL DEFAULT 0,
    remarks TEXT,
    scan_file TEXT, -- stored file name in the uploads folder
    scan_name TEXT, -- original file name
    scan_type TEXT, -- MIME type
    prepared_by TEXT,
    submitted_at TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    approved_by TEXT,
    approved_at TEXT,
    return_note TEXT,
    returned_by TEXT,
    UNIQUE(emp_id, payroll_month)
);

CREATE TABLE IF NOT EXISTS timesheet_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timesheet_id INTEGER NOT NULL REFERENCES timesheets(id),
    work_date TEXT NOT NULL,
    day_type TEXT NOT NULL, -- Work, Weekend, Public Holiday, Study Leave, Leave, Absent
    client_name TEXT,
    period_ended TEXT,
    nature_of_work TEXT,
    line_of_service TEXT,
    head_of_account TEXT,
    work_performed TEXT,
    job_code TEXT,
    hours REAL DEFAULT 0,
    UNIQUE(timesheet_id, work_date)
);

CREATE TABLE IF NOT EXISTS timesheet_codes ( -- working legends for the daily diary drop-downs
    category TEXT NOT NULL, -- line_of_service, head_of_account, work_performed
    code TEXT NOT NULL,
    description TEXT,
    added_by TEXT,
    added_at TEXT,
    PRIMARY KEY (category, code)
);

CREATE TABLE IF NOT EXISTS loan_applications ( -- staff apply; reviewer recommends; approver approves -> advances_loans
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    loan_type TEXT NOT NULL, -- Loan, Advance
    amount REAL NOT NULL,
    instalments INTEGER NOT NULL, -- Advance: 1 (recovered in full next month)
    purpose TEXT,
    applied_by TEXT,
    applied_at TEXT,
    policy_limit REAL, -- 2 x monthly salary at the time of applying
    outstanding REAL, -- loans still to be recovered + other pending applications at the time of applying
    exceeds_policy INTEGER DEFAULT 0, -- outstanding + amount > policy_limit: needs the reviewer's exception and the approver
    status TEXT NOT NULL DEFAULT 'Applied', -- Applied, Recommended, Approved, Rejected, Withdrawn
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT, -- the reviewer's justification (required for an exception) or reason for rejecting
    approved_amount REAL,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    loan_ref TEXT -- advances_loans record created on approval
);

CREATE TABLE IF NOT EXISTS holidays ( -- gazetted holidays (Saturday and Sunday are always off)
    holiday_date TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    added_by TEXT,
    added_at TEXT
);

CREATE TABLE IF NOT EXISTS leave_credits ( -- extra leave earned by working on a gazetted holiday / called in on a weekend
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    work_date TEXT NOT NULL,
    days REAL NOT NULL DEFAULT 1,
    reason TEXT NOT NULL, -- key of CREDIT_REASONS
    note TEXT,
    source TEXT NOT NULL DEFAULT 'Reviewer', -- Reviewer (entered by him) or Timesheet (gazetted holiday worked, from the diary)
    timesheet_id INTEGER,
    status TEXT NOT NULL DEFAULT 'Recommended', -- Recommended (reviewer) -> Approved / Rejected (approver)
    requested_by TEXT,
    requested_at TEXT,
    decided_by TEXT,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS mail_accounts ( -- each Approver/Reviewer/Preparer's own outgoing email account
    username TEXT PRIMARY KEY REFERENCES users(username),
    from_address TEXT NOT NULL,
    from_name TEXT,
    smtp_host TEXT NOT NULL,
    smtp_port INTEGER NOT NULL,
    security TEXT NOT NULL, -- STARTTLS, SSL, None
    smtp_user TEXT,
    smtp_password TEXT, -- app password; stored locally, never shown again
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS email_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL,
    sent_by TEXT NOT NULL,
    from_address TEXT,
    to_address TEXT,
    emp_id TEXT,
    purpose TEXT, -- Login details, Test
    status TEXT, -- Sent, Failed
    error TEXT
);

CREATE TABLE IF NOT EXISTS timesheet_releases (
    emp_id TEXT NOT NULL REFERENCES staff(emp_id),
    withheld_month TEXT NOT NULL, -- locked month whose salary was held for a missing timesheet
    amount REAL NOT NULL,
    release_month TEXT NOT NULL, -- Draft month it was paid out in after the timesheet was approved
    UNIQUE(emp_id, withheld_month)
);
"""

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA_SQL)
    # Columns added after the first release: CREATE TABLE IF NOT EXISTS won't add them to an existing database
    staff_cols = {r["name"] for r in conn.execute("PRAGMA table_info(staff)")}
    for col, col_type in (("eobi_registration_date", "TEXT"), ("weekly_hours", "REAL"), ("eobi_no", "TEXT"), ("date_of_birth", "TEXT"), ("icap_crn", "TEXT"), ("timesheet_from", "TEXT"), ("email", "TEXT"),
                          ("cnic_issue_date", "TEXT"), ("cnic_expiry", "TEXT"), ("attendance_code", "TEXT"), ("training_org", "TEXT")):
        if col not in staff_cols:
            conn.execute(f"ALTER TABLE staff ADD COLUMN {col} {col_type}")
    user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "role" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'Preparer'")
        conn.execute("UPDATE users SET role = 'Approver'")  # accounts from before roles existed are the firm's approver
    if "emp_id" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN emp_id TEXT")
    if "must_change_pw" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_pw INTEGER DEFAULT 0")
    credit_cols = {r["name"] for r in conn.execute("PRAGMA table_info(leave_credits)")}
    for col, col_type in (("applied_late", "INTEGER DEFAULT 0"), ("recommended_by", "TEXT"), ("recommended_at", "TEXT"),
                          ("condoned_by", "TEXT"), ("condoned_at", "TEXT"), ("condone_note", "TEXT")):
        if col not in credit_cols:
            conn.execute(f"ALTER TABLE leave_credits ADD COLUMN {col} {col_type}")
    if "arrival_time" not in {r["name"] for r in conn.execute("PRAGMA table_info(attendance)")}:
        conn.execute("ALTER TABLE attendance ADD COLUMN arrival_time TEXT")  # HH:MM from the attendance report
    line_cols = {r["name"] for r in conn.execute("PRAGMA table_info(payroll_lines)")}
    for col in ("salary_withheld_absence", "absence_salary_released", "salary_withheld_timesheet", "timesheet_salary_released",
                "leave_credit_days", "comp_leave_used", "leave_carried_forward", "leave_encash_days", "leave_encashment", "security_released",
                "salary_withheld_late", "late_salary_released"):
        if col not in line_cols:
            conn.execute(f"ALTER TABLE payroll_lines ADD COLUMN {col} REAL DEFAULT 0")
    if "imported_from" not in {r["name"] for r in conn.execute("PRAGMA table_info(payroll_runs)")}:
        conn.execute("ALTER TABLE payroll_runs ADD COLUMN imported_from TEXT")  # final sheet the month's figures came from
    if "entered_by" not in {r["name"] for r in conn.execute("PRAGMA table_info(leave_register)")}:
        conn.execute("ALTER TABLE leave_register ADD COLUMN entered_by TEXT")
    if "acting_for" not in {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}:
        conn.execute("ALTER TABLE sessions ADD COLUMN acting_for TEXT")
    loan_cols = {r["name"] for r in conn.execute("PRAGMA table_info(advances_loans)")}
    for col, col_type in (("disbursed", "INTEGER DEFAULT 1"), ("disbursed_at", "TEXT"), ("disbursed_by", "TEXT"), ("disbursement_batch", "TEXT")):
        if col not in loan_cols:
            conn.execute(f"ALTER TABLE advances_loans ADD COLUMN {col} {col_type}")  # loans already on file were paid outside the system
    travel_cols = {r["name"] for r in conn.execute("PRAGMA table_info(travel_claims)")}
    for col, col_type in (("status", "TEXT DEFAULT 'Submitted'"), ("chargeable", "INTEGER DEFAULT 0"), ("submitted_by", "TEXT"),
                          ("reviewed_by", "TEXT"), ("reviewed_at", "TEXT"), ("review_note", "TEXT"), ("invoiced", "INTEGER DEFAULT 0"),
                          ("invoice_no", "TEXT"), ("invoiced_by", "TEXT"), ("invoiced_at", "TEXT"),
                          ("travel_mode", "TEXT DEFAULT 'Own vehicle'"), ("fare", "REAL DEFAULT 0"), ("receipt_file", "TEXT"),
                          ("receipt_name", "TEXT"), ("receipt_type", "TEXT")):
        if col not in travel_cols:
            conn.execute(f"ALTER TABLE travel_claims ADD COLUMN {col} {col_type}")
    if conn.execute("SELECT COUNT(*) FROM doc_types").fetchone()[0] == 0:
        conn.executemany("INSERT INTO doc_types (code, name, applies_to, mandatory, has_expiry, added_by) VALUES (?,?,?,?,?,'system')", DEFAULT_DOC_TYPES)
    if conn.execute("SELECT COUNT(*) FROM timesheet_codes").fetchone()[0] == 0:
        for category, codes in TS_DEFAULT_CODES.items():
            conn.executemany("INSERT INTO timesheet_codes (category, code, added_by) VALUES (?, ?, 'system')",
                             [(category, c) for c in codes])
    conn.commit()
    conn.close()

# ==============================================================================
# 2. HELPER FUNCTIONS & DATE / CUT-OFF LOGIC
# ==============================================================================

def parse_date(val):
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def fmt_date(d):
    return d.strftime("%Y-%m-%d") if d else None

def first_of_month(d):
    return date(d.year, d.month, 1)

def add_months(d, months):
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last_day))

def eobi_start_date(staff_type, date_of_joining, icap_reg_date, eobi_reg_date=None, permanent_wait_months=3):
    """Rule: Interns never get EOBI. Anyone else already enrolled with EOBI starts from the
    EOBI registration date. Otherwise EOBI starts for Permanent staff after the waiting period
    from joining, and for ICAP Articles from the date ICAP confirms registration.
    Returns None when EOBI does not (yet) apply."""
    if staff_type == "Intern":
        return None
    if eobi_reg_date:
        return eobi_reg_date
    if staff_type == "Permanent":
        return add_months(date_of_joining, permanent_wait_months) if date_of_joining else None
    if staff_type in ARTICLE_TYPES:
        return icap_reg_date
    return None

STUDY_LEAVE_TYPE = "Exam / Study"  # never deducted from salary
ABSENCE_LEAVE_TYPE = "Unapproved absence"  # recorded by the Preparer/Approver; withholds the month's salary
EOBI_ONLY_TYPE = "EOBI Only"  # not an employee (e.g. partners, family): the firm pays their EOBI, nothing else
NOC_TYPE = "NOC Article"  # articleship transferred from another firm: residual leaves and residual period only
ARTICLE_TYPES = ("ICAP Article", NOC_TYPE)
STAFF_TYPE_OPTIONS = (("Permanent", "Permanent"), ("ICAP Article", "ICAP Articleship"), (NOC_TYPE, "NOC Articleship (transferred)"), ("Intern", "Internee"),
                      (EOBI_ONLY_TYPE, "EOBI Only (not an employee)"))
URGENT_WORK_PK = "Urgent work – within Pakistan"
URGENT_WORK_ABROAD = "Urgent work – outside Pakistan"
LEAVE_TYPES = ("Sick", STUDY_LEAVE_TYPE, URGENT_WORK_PK, URGENT_WORK_ABROAD, ABSENCE_LEAVE_TYPE)  # older records may still say Casual etc.
STAFF_LEAVE_TYPES = tuple(t for t in LEAVE_TYPES if t != ABSENCE_LEAVE_TYPE)  # what staff can apply for
NOT_DEDUCTED_TYPES = (STUDY_LEAVE_TYPE, URGENT_WORK_ABROAD)  # count towards the balance but are never deducted from salary
DEFAULT_DOC_TYPES = (  # code, name, applies to, mandatory, has expiry
    ("cnic", "CNIC (scanned, both sides)", "All", 1, 1),
    ("qualification", "Latest qualification document", "All", 1, 0),
    ("cv", "CV", "All", 1, 0),
    ("icap_card", "ICAP student card", "ICAP Article", 1, 0),
)
CNIC_WARN_DAYS = 60  # warn this many days before a CNIC expires

def cnic_alert(expiry, today=None):
    """'' if fine, else a short warning for an expired / soon-expiring CNIC."""
    d = parse_date(expiry)
    if d is None:
        return ""
    today = today or date.today()
    if d < today:
        return f"CNIC expired on {d.strftime('%d %b %Y')}"
    if (d - today).days <= CNIC_WARN_DAYS:
        return f"CNIC expires on {d.strftime('%d %b %Y')}"
    return ""

CREDIT_REASONS = {  # why extra leave was earned; ICAP students only for a gazetted holiday their client didn't allow
    "holiday_client": "Worked at client on gazetted holiday (client didn't allow it)",
    "holiday_called": "Called in to work on gazetted holiday",
    "weekend_called": "Called in to work on Saturday / Sunday",
}
TS_DAY_TYPES = ("Work", "Weekend", "Public Holiday", "Study Leave", "Leave", "Absent")
TS_OFF_DAYS = ("Weekend", "Public Holiday")  # not part of the standard hours
TS_TEXT_FIELDS = {  # timesheet_entries column -> daily diary heading
    "client_name": "Client's Name", "period_ended": "Year / Period Ended", "nature_of_work": "Nature of Work Undertaken / Training Received",
    "line_of_service": "Line of Service", "head_of_account": "Head of Account", "work_performed": "Work Performed", "job_code": "Job Code",
}
TS_CODE_FIELDS = ("line_of_service", "head_of_account", "work_performed")  # chosen from the working legends drop-downs
TS_DEFAULT_CODES = {  # codes listed on the daily diary template; the reviewer adds more and their descriptions
    "line_of_service": ("1100", "1200", "1300", "1400", "1500", "1600", "1610", "1620", "1630", "1640", "1650", "1660", "1670"),
    "head_of_account": ("2000", "2010", "2050", "2100", "2200", "2210", "2250", "2300", "2400", "2500", "2600", "2610", "2620",
                        "2630", "2640", "2650", "2660", "2670", "2680", "2700"),
    "work_performed": ("3000", "3100", "3200", "3300", "3400", "3500", "3600", "3700"),
}

def pay_month_label(payroll_month_str):
    return parse_date(payroll_month_str).strftime("%B %Y")

def get_payroll_month_for_event(event_date, cutoff_day=25):
    """Rule: Leave or travel dated after the 25th falls into next month's payroll."""
    if event_date.day > cutoff_day:
        return add_months(first_of_month(event_date), 1)
    return first_of_month(event_date)

def get_settings_dict(conn):
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}

# ==============================================================================
# 3. CORE PAYROLL CALCULATION ENGINE
# ==============================================================================

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

def is_month_locked(conn, payroll_month_str):
    """Monthly sign-off runs Draft -> Prepared -> Reviewed -> Approved. From Prepared onward the month is locked:
    its figures are never recalculated and nothing can be entered for it until it is sent back to Draft."""
    row = conn.execute("SELECT status FROM payroll_runs WHERE payroll_month = ?", (payroll_month_str,)).fetchone()
    return row is not None and row["status"] != "Draft"

def close_leavers(conn):
    """Staff whose leaving date has passed become 'Left' (until then they stay Active and are paid up to that date)."""
    conn.execute("UPDATE staff SET status = 'Left' WHERE status = 'Active' AND leaving_date IS NOT NULL AND leaving_date < ?",
                 (date.today().isoformat(),))

def weekly_hours_for(conn, staff, pm_str):
    """A part-timer's weekly hours for a payroll month: the latest monthly update up to that month, otherwise Staff Master. None = full time."""
    row = conn.execute("""SELECT weekly_hours FROM part_time_hours WHERE emp_id = ? AND payroll_month <= ?
                          ORDER BY payroll_month DESC LIMIT 1""", (staff["emp_id"], pm_str)).fetchone()
    return (row["weekly_hours"] if row else staff["weekly_hours"]) or None

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULT_OFF_DAYS = "5,6"  # Saturday and Sunday

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
        if stype == "Intern" and INTERN_OLD_RATE and pm_str < INTERN_OLD_RATE[0]:
            monthly_sal = INTERN_OLD_RATE[1]  # the stipend in force before the revision
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
        for ln in loans:
            amt = float(ln["amount"])
            inst = amt if ln["loan_type"] == "Advance" else float(ln["monthly_instalment"] or 0)
            first_rec = parse_date(ln["first_recovery_month"])
            hold_prior = int(ln["months_on_hold_prior"] or 0)
            hold_now = int(ln["hold_this_month"] or 0)

            if not first_rec or pay_month < first_rec or hold_now == 1:
                rec_this = 0.0
            else:
                months_elapsed = max(0, (pay_month.year - first_rec.year) * 12 + (pay_month.month - first_rec.month))
                recovered_before = min(amt, inst * max(0, months_elapsed - hold_prior))
                rec_this = min(inst, max(0.0, amt - recovered_before))
            loan_recovery += rec_this

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

# ==============================================================================
# 4. BANK FILE EXPORTS (IBFT & MEEZAN IFT)
# ==============================================================================

def generate_bank_files(payroll_month_str, out_dir=None):
    if out_dir is None:
        out_dir = EXPORT_DIR
    os.makedirs(out_dir, exist_ok=True)
    conn = get_conn()
    pm = first_of_month(parse_date(payroll_month_str))
    pm_str = fmt_date(pm)
    mon_tag = pm.strftime("%b%y")

    ibft_rows = conn.execute("""
        SELECT p.emp_id, p.employee_name, p.net_pay, s.bank_account_iban, s.account_title, b.ibft_bank_code
        FROM payroll_lines p
        JOIN staff s ON p.emp_id = s.emp_id
        LEFT JOIN banks b ON s.bank_name = b.bank_name
        WHERE p.payroll_month = ? AND p.payment_mode = 'IBFT' AND p.net_pay > 0
        ORDER BY p.emp_id
    """, (pm_str,)).fetchall()

    ibft_path = os.path.join(out_dir, f"IBFT_{pm.strftime('%b_%Y')}.csv")
    write_ibft_file(ibft_path, [(r, r["net_pay"], "".join(r["employee_name"].split())[:14] + mon_tag) for r in ibft_rows])

    ift_rows = conn.execute("""
        SELECT p.emp_id, p.employee_name, p.net_pay, s.bank_account_iban, s.account_title
        FROM payroll_lines p
        JOIN staff s ON p.emp_id = s.emp_id
        WHERE p.payroll_month = ? AND p.payment_mode = 'Meezan IFT' AND p.net_pay > 0
        ORDER BY p.emp_id
    """, (pm_str,)).fetchall()

    ift_path = os.path.join(out_dir, f"Meezan_IFT_{pm.strftime('%b_%Y')}.csv")
    write_ift_file(ift_path, [(r, r["net_pay"], f"Salary {pm.strftime('%b-%y')} {r['emp_id']}") for r in ift_rows])

    conn.close()
    return ibft_path, ift_path

def write_ibft_file(path, items):
    """IBFT upload file; items = [(row with employee_name / bank_account_iban / account_title / ibft_bank_code, amount, reference)]."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ACCOUNT NUMBER", "BENE NAME", "CUSTOMER REFERENCE NUMBER", "TRANS AMOUNT", "BANK CODE", "BENEFICIARY ACCOUNT TITLE", "ADVICING"])
        for r, amount, ref in items:
            w.writerow([r["bank_account_iban"], r["employee_name"], ref, int(round(amount)), r["ibft_bank_code"] or "",
                        r["account_title"] or r["employee_name"], ""])

def write_ift_file(path, items):
    """Meezan IFT upload file; items = [(row with employee_name / bank_account_iban / account_title, amount, narration)]."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ACCOUNT NUMBER (last 10 digits)", "ACCOUNT TITLE", "AMOUNT", "NARRATION / REFERENCE"])
        for r, amount, narr in items:
            w.writerow(["".join((r["bank_account_iban"] or "").split())[-10:], r["account_title"] or r["employee_name"], int(round(amount)), narr])

def loans_to_disburse(conn):
    """Approved loans / advances not yet paid out, with the staff bank details the bank files need."""
    return conn.execute("""
        SELECT a.loan_ref, a.emp_id, a.loan_type, a.amount, a.approval_date, s.employee_name, s.bank_name, s.bank_account_iban,
               s.account_title, b.ibft_bank_code, COALESCE(b.is_meezan, 0) AS is_meezan
        FROM advances_loans a JOIN staff s ON s.emp_id = a.emp_id LEFT JOIN banks b ON b.bank_name = s.bank_name
        WHERE a.disbursed = 0 ORDER BY a.loan_ref
    """).fetchall()

def generate_loan_bank_files(conn, user, out_dir=None):
    """Separate IBFT / Meezan IFT files paying out approved loans and advances (never mixed with salaries).
    Each loan paid is marked disbursed with the batch number, so it can't be paid twice. Loans whose staff member has no
    bank account (or no IBFT bank code) are left for the next batch. Returns (batch, ibft_path, ift_path, paid, skipped)."""
    out_dir = out_dir or os.path.join(EXPORT_DIR, "loans")
    os.makedirs(out_dir, exist_ok=True)
    now = datetime.now()
    batch = f"LD{now.strftime('%Y%m%d-%H%M%S')}"
    ibft, ift, skipped = [], [], []
    for r in loans_to_disburse(conn):
        meezan = r["is_meezan"] == 1 or r["bank_name"] == "Meezan Bank"
        if not r["bank_account_iban"] or (not meezan and not r["ibft_bank_code"]):
            skipped.append(r)
        elif meezan:
            ift.append((r, r["amount"], f"{r['loan_type']} {r['loan_ref']} {r['emp_id']}"))
        else:
            ibft.append((r, r["amount"], f"{r['loan_type'][:4]}{r['loan_ref']}"))
    ibft_path = ift_path = None
    if ibft:
        ibft_path = os.path.join(out_dir, f"IBFT_Loans_{batch}.csv")
        write_ibft_file(ibft_path, ibft)
    if ift:
        ift_path = os.path.join(out_dir, f"Meezan_IFT_Loans_{batch}.csv")
        write_ift_file(ift_path, ift)
    paid = [r for r, _, _ in ibft + ift]
    for r in paid:
        conn.execute("UPDATE advances_loans SET disbursed = 1, disbursed_at = ?, disbursed_by = ?, disbursement_batch = ? WHERE loan_ref = ?",
                     (now.isoformat(timespec="seconds"), user, batch, r["loan_ref"]))
    return batch, ibft_path, ift_path, paid, skipped

# ==============================================================================
# 5. ONE-TIME MIGRATION FROM EXCEL WORKBOOK
# ==============================================================================

def seed_from_excel(xlsx_path=None):
    xlsx_path = xlsx_path or EXCEL_SOURCE
    preparer, reviewer, approver = COMPANIES[COMPANY_KEY]["officers"]
    conn = get_conn()
    default_settings = [
        ("firm_name", FIRM_NAME, "Firm legal name"),
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
        ("app_url", f"http://127.0.0.1:{PORT}", "Payroll address staff open in their browser (put in login emails); change when deployed"),
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

# ==============================================================================
# 6. AUTHENTICATION (users, password hashing, sessions)
# ==============================================================================

SESSION_COOKIE = "sic_session"
SESSION_HOURS = 8
PBKDF2_ROUNDS = 200_000
MIN_PASSWORD_LEN = 8

def hash_password(password, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return salt.hex(), digest.hex()

def user_count():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return n

ROLE_APPROVER = "Approver"  # approves staff/pay changes and each month's payroll
ROLE_REVIEWER = "Reviewer"  # reviews the prepared payroll; approves or rejects leave applications
ROLE_PREPARER = "Preparer"  # enters data and prepares the payroll; pay changes wait for approval
ROLE_STAFF = "Staff"        # applies for own leave and submits own travel claims, nothing else
ROLES = (ROLE_APPROVER, ROLE_REVIEWER, ROLE_PREPARER, ROLE_STAFF)
ROLE_TABS = {  # tabs each role may open
    ROLE_APPROVER: ("payroll", "attendance", "staff", "leaves", "holidays", "timesheets", "travel", "loans", "inputs", "approvals", "users", "email", "account"),
    ROLE_REVIEWER: ("payroll", "attendance", "staff", "leaves", "holidays", "timesheets", "travel", "loans", "inputs", "approvals", "email", "account"),
    ROLE_PREPARER: ("payroll", "attendance", "staff", "leaves", "holidays", "travel", "loans", "inputs", "approvals", "email", "account"),
    ROLE_STAFF: ("leaves", "holidays", "timesheets", "travel", "loans", "account"),  # timesheets only for ICAP students / staff asked by the reviewer (see do_GET)
}
ROLE_ACTIONS = {  # POST paths each role may use
    "/add_leave": ROLES, "/add_travel": ROLES, "/change_password": ROLES,
    "/save_timesheet": (ROLE_APPROVER, ROLE_STAFF),  # the student, or the approver on their behalf
    "/decide_timesheet": (ROLE_APPROVER, ROLE_REVIEWER), "/save_ts_code": (ROLE_APPROVER, ROLE_REVIEWER),
    "/set_ts_access": (ROLE_APPROVER, ROLE_REVIEWER),  # require timesheets from a non-ICAP staff member
    "/upload_emails": (ROLE_APPROVER, ROLE_REVIEWER, ROLE_PREPARER), "/set_email": (ROLE_APPROVER, ROLE_REVIEWER, ROLE_PREPARER),
    "/save_mail_account": (ROLE_APPROVER, ROLE_REVIEWER, ROLE_PREPARER), "/send_test_email": (ROLE_APPROVER, ROLE_REVIEWER, ROLE_PREPARER),
    "/send_logins": (ROLE_APPROVER, ROLE_REVIEWER, ROLE_PREPARER), "/set_app_url": (ROLE_APPROVER, ROLE_REVIEWER, ROLE_PREPARER),
    "/set_approver_email": (ROLE_APPROVER, ROLE_REVIEWER),
    "/add_holiday": (ROLE_APPROVER, ROLE_REVIEWER), "/delete_holiday": (ROLE_APPROVER, ROLE_REVIEWER),
    "/email_holidays": (ROLE_APPROVER, ROLE_REVIEWER),
    "/add_credit": (ROLE_APPROVER, ROLE_REVIEWER), "/decide_credit": (ROLE_APPROVER,),
    "/apply_credit": ROLES, "/review_credit": (ROLE_APPROVER, ROLE_REVIEWER),  # own application; the reviewer recommends or condones
    "/apply_loan": ROLES, "/withdraw_loan": ROLES,  # own application (every user linked to a staff member)
    "/review_loan": (ROLE_REVIEWER,), "/decide_loan": (ROLE_APPROVER,),
    "/export_loans": (ROLE_APPROVER, ROLE_REVIEWER, ROLE_PREPARER),  # separate loan IBFT / IFT files for approved loans
    "/review_travel": (ROLE_APPROVER, ROLE_REVIEWER), "/mark_invoiced": (ROLE_APPROVER, ROLE_REVIEWER),
    "/fetch_fuel_prices": (ROLE_APPROVER, ROLE_REVIEWER), "/add_fuel_price": (ROLE_APPROVER,),
    "/withdraw_travel": ROLES,  # own submitted claim (or the preparer/approver for anyone), before review
    "/update_staff": (ROLE_APPROVER, ROLE_PREPARER), "/add_staff": (ROLE_APPROVER, ROLE_PREPARER),
    "/add_input": (ROLE_APPROVER, ROLE_PREPARER), "/add_loan": (ROLE_APPROVER, ROLE_PREPARER),
    "/prepare_month": (ROLE_APPROVER, ROLE_PREPARER),
    "/decide_leave": (ROLE_APPROVER, ROLE_REVIEWER), "/bulk_leave": (ROLE_APPROVER, ROLE_REVIEWER),
    "/set_travel_rate": (ROLE_APPROVER,), "/release_salary": (ROLE_APPROVER,), "/release_late_hold": (ROLE_APPROVER,),  # the formula: approver only
    "/set_rate_uplift": (ROLE_APPROVER, ROLE_REVIEWER),  # reviewer: increase of at most 10% with a written reason
    "/review_month": (ROLE_APPROVER, ROLE_REVIEWER), "/send_back_month": (ROLE_APPROVER, ROLE_REVIEWER),
    "/approve_month": (ROLE_APPROVER,), "/approve_request": (ROLE_APPROVER,), "/reject_request": (ROLE_APPROVER,),
    "/add_user": (ROLE_APPROVER,), "/delete_user": (ROLE_APPROVER,),
    "/set_delegate": (ROLE_APPROVER,), "/remove_delegate": (ROLE_APPROVER,),
    "/upload_document": (ROLE_APPROVER, ROLE_PREPARER),
    "/upload_attendance": (ROLE_PREPARER, ROLE_APPROVER), "/map_attendance": (ROLE_PREPARER, ROLE_APPROVER),
    "/absence_action": (ROLE_PREPARER, ROLE_APPROVER),
    "/save_timing": (ROLE_REVIEWER, ROLE_APPROVER), "/delete_timing": (ROLE_REVIEWER, ROLE_APPROVER),  # individual office timings
    "/save_part_hours": (ROLE_REVIEWER, ROLE_APPROVER),  # part-timers' weekly hours, updated each month
    "/settle_security": (ROLE_APPROVER,),  # held pay / security of a leaver: only the approver releases or forfeits it
    "/leave_import_upload": (ROLE_APPROVER,), "/leave_import_apply": (ROLE_APPROVER,), "/leave_import_discard": (ROLE_APPROVER,),
    "/import_payroll_upload": (ROLE_APPROVER,), "/import_payroll_apply": (ROLE_APPROVER,), "/import_payroll_discard": (ROLE_APPROVER,),
    "/save_doc_type": (ROLE_APPROVER,), "/toggle_doc_type": (ROLE_APPROVER,),  # only the approver decides required documents  # who operates the portal of staff who can't
}

def create_user(username, password, role=ROLE_PREPARER, emp_id=None, must_change=False):
    salt_hex, hash_hex = hash_password(password)
    conn = get_conn()
    conn.execute("INSERT INTO users (username, password_salt, password_hash, created_at, role, emp_id, must_change_pw) VALUES (?,?,?,?,?,?,?)",
                 (username, salt_hex, hash_hex, datetime.now().isoformat(timespec="seconds"), role, emp_id, 1 if must_change else 0))
    conn.commit()
    conn.close()

def get_user(username):
    conn = get_conn()
    row = conn.execute("SELECT username, role, emp_id, must_change_pw FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return row

def normalise_username(username):
    """Staff user IDs are their CNIC without dashes; accept it typed with dashes too."""
    digits = username.replace("-", "").replace(" ", "")
    return digits if len(digits) == 13 and digits.isdigit() else username

def set_password(username, password):
    salt_hex, hash_hex = hash_password(password)
    conn = get_conn()
    conn.execute("UPDATE users SET password_salt = ?, password_hash = ?, must_change_pw = 0 WHERE username = ?", (salt_hex, hash_hex, username))
    conn.commit()
    conn.close()

def delete_user(username):
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
    conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    conn.close()

def staff_access_ended(row):
    """A leaver keeps access through their last working day; with no leaving date on record, access ends once marked Left."""
    if row["leaving_date"]:
        return row["leaving_date"] < date.today().isoformat()
    return row["status"] == "Left"

def access_removed(conn, username):
    row = conn.execute("""SELECT s.status, s.leaving_date FROM users u JOIN staff s ON s.emp_id = u.emp_id
                          WHERE u.username = ?""", (username,)).fetchone()
    return row is not None and staff_access_ended(row)

def verify_login(username, password):
    conn = get_conn()
    row = conn.execute("SELECT password_salt, password_hash FROM users WHERE username = ?", (username,)).fetchone()
    removed = access_removed(conn, username)
    conn.close()
    if row is None:
        hash_password(password)  # same work for unknown users, so timing doesn't reveal valid usernames
        return False
    _, hash_hex = hash_password(password, row["password_salt"])
    return hmac.compare_digest(hash_hex, row["password_hash"]) and not removed

def create_session(username):
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.now().isoformat(timespec="seconds"),))
    conn.execute("INSERT INTO sessions (token, username, expires_at) VALUES (?,?,?)", (token, username, expires))
    conn.commit()
    conn.close()
    return token

def get_session_user(token):
    if not token:
        return None
    conn = get_conn()
    row = conn.execute("SELECT username, expires_at FROM sessions WHERE token = ?", (token,)).fetchone()
    removed = row is not None and access_removed(conn, row["username"])  # a leaver already logged in is signed out too
    conn.close()
    if row is None or removed or row["expires_at"] < datetime.now().isoformat(timespec="seconds"):
        return None
    return row["username"]

def set_session_acting(token, emp_id):
    conn = get_conn()
    conn.execute("UPDATE sessions SET acting_for = ? WHERE token = ?", (emp_id, token))
    conn.commit()
    conn.close()

def delegated_portals(conn, username):
    """Staff whose portal this user operates: [(emp_id, employee_name)]."""
    return conn.execute("""SELECT d.emp_id, s.employee_name FROM portal_delegates d JOIN staff s ON s.emp_id = d.emp_id
                           JOIN users u ON u.emp_id = d.emp_id WHERE d.delegate_username = ? ORDER BY d.emp_id""", (username,)).fetchall()

def approver_only_emps(conn):
    """Staff whose leave, travel, loans and timesheets the reviewer must not decide, so they go straight to the approver:
    the reviewer himself, and staff whose portal the reviewer operates for them."""
    return {r[0] for r in conn.execute("""
        SELECT emp_id FROM users WHERE role = ? AND emp_id IS NOT NULL
        UNION SELECT d.emp_id FROM portal_delegates d JOIN users u ON u.username = d.delegate_username WHERE u.role = ?
    """, (ROLE_REVIEWER, ROLE_REVIEWER))}

def delete_session(token):
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()

# ==============================================================================
# 7. CHANGE APPROVAL (new staff, staff/pay changes, adjustments, loans)
# ==============================================================================
# Anything that adds a person to the payroll or raises/lowers pay is a change request. A Preparer's request
# waits for the Approver; the Approver's own changes apply at once. Both are kept in change_requests as a log.

class ChangeError(Exception):
    """Raised with a FLASH_MESSAGES key when a submitted change can't be accepted."""

KIND_LABELS = {"add_staff": "New staff member", "update_staff": "Staff / pay change",
               "add_input": "Monthly adjustment", "add_loan": "Advance / loan",
               "travel_rate": "Travel rate per km", "salary_release": "Withheld salary released", "late_hold_release": "Late-arrival hold released", "staff_timing": "Staff office timing", "part_time_hours": "Part-timer weekly hours",
               "ts_code": "Timesheet code (working legend)", "ts_access": "Timesheets required (non-ICAP staff)",
               "staff_email": "Staff email addresses", "portal_delegate": "Portal operated for staff",
               "staff_document": "Staff document", "doc_type": "Required documents", "payroll_import": "Final payroll sheet",
               "attendance": "Attendance report", "leave_import": "Leave history upload", "security": "Held pay / security settled", "holiday": "Gazetted holiday", "leave_credit": "Leave credit (holiday / weekend worked)"}

STAFF_EDITABLE = {  # column -> label
    "staff_type": "Staff Type", "date_of_joining": "Date of Joining", "monthly_salary": "Monthly Salary",
    "weekly_hours": "Weekly Hours", "icap_reg_confirmed_date": "ICAP Registration Date",
    "eobi_registration_date": "EOBI Registration Date", "status": "Status", "leaving_date": "Leaving Date",
    "eobi_no": "EOBI No.", "date_of_birth": "Date of Birth", "icap_crn": "ICAP CRN", "training_org": "ICAP Training Organisation",
    "cnic_issue_date": "CNIC Issue Date", "cnic_expiry": "CNIC Expiry",
    "leave_entitlement_override": "Residual Leaves (NOC)", "articleship_end_date": "Residual Period Ends (NOC)",
}
NEW_STAFF_LABELS = {  # column -> label, for the add-staff form
    "employee_name": "Name", "cnic": "CNIC", "designation": "Designation", "staff_type": "Staff Type",
    "date_of_joining": "Date of Joining", "monthly_salary": "Monthly Salary", "weekly_hours": "Weekly Hours",
    "bank_name": "Bank", "bank_account_iban": "IBAN", "account_title": "Account Title",
    "eobi_no": "EOBI No.", "date_of_birth": "Date of Birth", "eobi_registration_date": "EOBI Registration Date",
    "icap_crn": "ICAP CRN", "cnic_issue_date": "CNIC Issue Date", "cnic_expiry": "CNIC Expiry",
    "icap_reg_confirmed_date": "ICAP Card Received", "training_org": "ICAP Training Organisation",
    "leave_entitlement_override": "Residual Leaves (NOC)", "articleship_end_date": "Residual Period Ends (NOC)",
}
INPUT_LABELS = {"bonus": "Bonus", "arrears_other": "Arrears",
                "partial_days_client_deduction": "Client Ded.", "leave_days_waived": "Waived Leaves"}

def fmt_change_value(col, val):
    if val is None or val == "":
        return "(blank)"
    if col == "monthly_salary":
        return f"Rs. {float(val):,.0f}"
    if col == "weekly_hours":
        return f"{float(val):g} h"
    return str(val)

def training_org_of(staff):
    """The firm ICAP records this student's training under (not necessarily the firm that pays them)."""
    return staff["training_org"] or TRAINING_ORGS[0]

def normalise_hours(value, cfg):
    hours = float(value)
    return None if hours >= float(cfg.get("standard_weekly_hours", 45)) else hours

def build_change(conn, kind, data, pm_str, cfg):
    """Validate a submitted form; returns (emp_id, payroll_month, payload, summary)."""
    emp_id = data.get("emp_id", "").strip().upper()
    staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()

    if kind == "add_staff":
        eobi_only = data.get("staff_type") == EOBI_ONLY_TYPE
        # Employees need a joining date and salary; EOBI-only people need just their EOBI registration date
        if (not emp_id or not data.get("employee_name", "").strip()
                or (not eobi_only and (not data.get("monthly_salary") or not data.get("date_of_joining")))
                or (eobi_only and not data.get("eobi_registration_date"))):
            raise ChangeError("bad_input")
        if staff:
            raise ChangeError("emp_exists")
        fields = {"emp_id": emp_id, "status": "Active"}
        for col in NEW_STAFF_LABELS:
            val = data.get(col, "").strip()
            if val:
                fields[col] = val
        fields["monthly_salary"] = 0.0 if eobi_only else float(fields["monthly_salary"])
        if data.get("staff_type") in ARTICLE_TYPES:
            fields["training_org"] = fields.get("training_org", TRAINING_ORGS[0])
            if fields["training_org"] not in TRAINING_ORGS:
                raise ChangeError("bad_input")
        else:
            fields.pop("training_org", None)
        if data.get("staff_type") == NOC_TYPE:  # a transferred articleship: only what remains of the leaves and the period
            if not parse_date(fields.get("articleship_end_date")) or "leave_entitlement_override" not in fields:
                raise ChangeError("noc_needs_residual")
            fields["leave_entitlement_override"] = float(fields["leave_entitlement_override"])
        else:
            fields.pop("leave_entitlement_override", None)
            fields.pop("articleship_end_date", None)
        if "weekly_hours" in fields:
            fields["weekly_hours"] = normalise_hours(fields["weekly_hours"], cfg)
        summary = f"{emp_id} — " + "; ".join(f"{NEW_STAFF_LABELS[c]}: {fmt_change_value(c, v)}"
                                             for c, v in fields.items() if c in NEW_STAFF_LABELS)
        return emp_id, None, {"fields": fields}, summary

    if not staff:
        raise ChangeError("emp_missing")
    name = staff["employee_name"]

    if kind == "update_staff":
        changes = {}
        for col in STAFF_EDITABLE:
            val = data.get(col, "").strip()
            if not val:
                continue
            if col == "training_org":
                if val not in TRAINING_ORGS:
                    raise ChangeError("bad_input")
                if (data.get("staff_type") or staff["staff_type"]) not in ARTICLE_TYPES:
                    continue  # only ICAP and NOC students are registered with a training organisation
            if col in ("monthly_salary", "leave_entitlement_override"):
                val = float(val)
            elif col == "weekly_hours":
                val = normalise_hours(val, cfg)
            if val != staff[col]:
                changes[col] = val
        if not changes:
            raise ChangeError("nothing_changed")
        summary = f"{emp_id} {name}: " + "; ".join(
            f"{STAFF_EDITABLE[c]} {fmt_change_value(c, staff[c])} → {fmt_change_value(c, v)}" for c, v in changes.items())
        return emp_id, None, {"emp_id": emp_id, "changes": changes}, summary

    if kind == "add_input":
        if is_month_locked(conn, pm_str):
            raise ChangeError("locked")
        values = {c: float(data.get(c) or 0) for c in INPUT_LABELS}
        existing = conn.execute("SELECT 1 FROM monthly_inputs WHERE payroll_month = ? AND emp_id = ?", (pm_str, emp_id)).fetchone()
        parts = [f"{INPUT_LABELS[c]} {v:g}" if c == "leave_days_waived" else f"{INPUT_LABELS[c]} Rs. {v:,.0f}"
                 for c, v in values.items() if v]
        summary = (f"{emp_id} {name} — {pay_month_label(pm_str)}: " + ("; ".join(parts) or "all amounts zero")
                   + (" (replaces existing entry)" if existing else ""))
        return emp_id, pm_str, {"emp_id": emp_id, "payroll_month": pm_str, "values": values, "note": data.get("note", "")}, summary

    if kind == "add_loan":
        ref = data.get("loan_ref", "").strip().upper()
        given = parse_date(data.get("date_given"))
        if not ref or not given or not data.get("amount"):
            raise ChangeError("bad_input")
        loan_type = data.get("loan_type") if data.get("loan_type") in ("Loan", "Advance") else "Loan"
        amount = float(data["amount"])
        instalment = float(data.get("monthly_instalment") or 0)
        first_m = fmt_date(add_months(first_of_month(given), 1))
        existing = conn.execute("SELECT 1 FROM advances_loans WHERE loan_ref = ?", (ref,)).fetchone()
        if loan_type == "Loan" and (instalment <= 0 or -(-amount // instalment) > int(float(cfg.get("loan_max_instalments", 20)))):
            raise ChangeError("loan_instalments")
        limit, outstanding, exceeds = loan_policy(conn, staff, 0 if existing else amount, first_m, cfg)
        recovery = f"Rs. {instalment:,.0f}/month" if loan_type == "Loan" else "recovered in full"
        summary = (f"{emp_id} {name} — {loan_type} {ref}: Rs. {amount:,.0f}, {recovery} from {pay_month_label(first_m)}"
                   + (" (replaces existing loan)" if existing else "")
                   + (f" — EXCEEDS POLICY: outstanding would be Rs. {outstanding + amount:,.0f} against the limit of Rs. {limit:,.0f}" if exceeds else ""))
        payload = {"loan_ref": ref, "emp_id": emp_id, "loan_type": loan_type, "date_given": fmt_date(given),
                   "amount": amount, "monthly_instalment": instalment, "first_recovery_month": first_m,
                   "purpose": data.get("purpose", "")}
        return emp_id, first_m, payload, summary

    raise ChangeError("bad_input")

def apply_change(conn, kind, payload):
    if kind == "add_staff":
        fields = {c: v for c, v in payload["fields"].items() if c in NEW_STAFF_LABELS or c in ("emp_id", "status")}
        if conn.execute("SELECT 1 FROM staff WHERE emp_id = ?", (fields["emp_id"],)).fetchone():
            raise ChangeError("emp_exists")
        conn.execute(f"INSERT INTO staff ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})", list(fields.values()))
    elif kind == "update_staff":
        for col, val in payload["changes"].items():
            if col in STAFF_EDITABLE:  # column names come only from this whitelist
                conn.execute(f"UPDATE staff SET {col} = ? WHERE emp_id = ?", (val, payload["emp_id"]))
    elif kind == "add_input":
        if is_month_locked(conn, payload["payroll_month"]):
            raise ChangeError("locked")
        v = payload["values"]
        conn.execute("""
            INSERT OR REPLACE INTO monthly_inputs (payroll_month, emp_id, bonus, arrears_other, late_arrival_deduction, partial_days_client_deduction, leave_days_waived, note)
            VALUES (?,?,?,?,?,?,?,?)
        """, (payload["payroll_month"], payload["emp_id"], v["bonus"], v["arrears_other"], 0.0,
              v["partial_days_client_deduction"], v["leave_days_waived"], payload["note"]))
    elif kind == "add_loan":
        p = payload
        conn.execute("""
            INSERT OR REPLACE INTO advances_loans (loan_ref, emp_id, loan_type, date_given, amount, monthly_instalment, first_recovery_month, purpose)
            VALUES (?,?,?,?,?,?,?,?)
        """, (p["loan_ref"], p["emp_id"], p["loan_type"], p["date_given"], p["amount"], p["monthly_instalment"],
              p["first_recovery_month"], p["purpose"]))

def submit_change(conn, kind, data, pm_str, cfg, user, role):
    """Approver: apply now. Preparer: queue for approval. Returns a FLASH_MESSAGES key."""
    emp_id, month, payload, summary = build_change(conn, kind, data, pm_str, cfg)
    now = datetime.now().isoformat(timespec="seconds")
    approved = role == ROLE_APPROVER
    if approved:
        apply_change(conn, kind, payload)
    conn.execute("""
        INSERT INTO change_requests (kind, emp_id, payroll_month, payload, summary, requested_by, requested_at, status, decided_by, decided_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (kind, emp_id, month, json.dumps(payload), summary, user, now,
          "Approved" if approved else "Pending", user if approved else None, now if approved else None))
    return "applied" if approved else "submitted"

def decide_change(conn, request_id, approve, user):
    req = conn.execute("SELECT * FROM change_requests WHERE id = ? AND status = 'Pending'", (request_id,)).fetchone()
    if req is None:
        raise ChangeError("bad_input")
    if approve:
        apply_change(conn, req["kind"], json.loads(req["payload"]))
    elif req["kind"] == "add_staff":
        delete_request_documents(conn, request_id)  # the person is not added, so their documents aren't kept
    conn.execute("UPDATE change_requests SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                 ("Approved" if approve else "Rejected", user, datetime.now().isoformat(timespec="seconds"), request_id))
    return "req_approved" if approve else "req_rejected"

def log_decision(conn, kind, emp_id, month, payload, summary, user):
    """Record an action that needs no separate approval (rate change, salary release) in the approval log."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        INSERT INTO change_requests (kind, emp_id, payroll_month, payload, summary, requested_by, requested_at, status, decided_by, decided_at)
        VALUES (?,?,?,?,?,?,?,'Approved',?,?)
    """, (kind, emp_id, month, json.dumps(payload), summary, user, now, user, now))

def decide_leave(conn, leave, decision, user, role, own_emp):
    """Leave chain: Pending -> Recommended (Reviewer) -> Approved (Approver); either may reject.
    Nobody decides their own leave, so a Reviewer's own leave (and that of staff whose portal he operates) goes straight
    to the Approver. A recorded absence isn't an application: it can only be cancelled if entered by mistake."""
    approver_only = leave["emp_id"] in approver_only_emps(conn)
    if (own_emp and leave["emp_id"] == own_emp) or (role == ROLE_REVIEWER and approver_only and leave["status"] != "Recorded"):
        raise ChangeError("own_leave")
    if is_month_locked(conn, leave["payroll_month"]):
        raise ChangeError("locked")
    status = leave["status"]
    applicant_is_reviewer = approver_only
    allowed = {
        "Recommended": role == ROLE_REVIEWER and status == "Pending",
        "Approved": role == ROLE_APPROVER and (status == "Recommended" or (status == "Pending" and applicant_is_reviewer)),
        "Rejected": status in ("Pending", "Recommended"),
        "Cancelled": status == "Recorded",
    }.get(decision, False)
    if not allowed:
        raise ChangeError("wrong_leave_stage")
    by_col = "recommended_by" if decision == "Recommended" else "approved_by"
    conn.execute(f"UPDATE leave_register SET status = ?, {by_col} = ? WHERE id = ?", (decision, user, leave["id"]))

def check_entry(conn, emp_id, payroll_month):
    """Leaves and travel need no pre-approval, but must be for existing staff and an open (unapproved) month."""
    staff = conn.execute("SELECT staff_type FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
    if not staff:
        raise ChangeError("emp_missing")
    if staff["staff_type"] == EOBI_ONLY_TYPE:
        raise ChangeError("not_employee")
    if is_month_locked(conn, payroll_month):
        raise ChangeError("locked")

def effective_salary(conn, staff_row, pm_str, cfg):
    """Monthly salary actually paid in a month (part-timers pro-rated on that month's hours against the 45-hour week)."""
    sal = float(staff_row["monthly_salary"] or 0)
    hours = weekly_hours_for(conn, staff_row, pm_str)
    if hours:
        sal = round(sal * float(hours) / float(cfg.get("standard_weekly_hours", 45)), 0)
    return sal

def loan_outstanding(conn, emp_id, as_of_month):
    """Loans / advances still to be recovered at the start of `as_of_month` (same recovery rules as the payroll),
    plus loan applications still waiting for a decision."""
    as_of = parse_date(as_of_month)
    total = 0.0
    for ln in conn.execute("SELECT * FROM advances_loans WHERE emp_id = ?", (emp_id,)):
        amt = float(ln["amount"])
        inst = amt if ln["loan_type"] == "Advance" else float(ln["monthly_instalment"] or 0)
        first_rec = parse_date(ln["first_recovery_month"])
        recovered = 0.0
        if first_rec and as_of >= first_rec:
            elapsed = (as_of.year - first_rec.year) * 12 + (as_of.month - first_rec.month)
            recovered = min(amt, inst * max(0, elapsed - int(ln["months_on_hold_prior"] or 0)))
        total += max(0.0, amt - recovered)
    total += float(conn.execute("SELECT COALESCE(SUM(amount), 0) FROM loan_applications WHERE emp_id = ? AND status IN ('Applied', 'Recommended')",
                                (emp_id,)).fetchone()[0])
    return total

def loan_policy(conn, staff_row, amount, as_of_month, cfg):
    """(limit, outstanding, exceeds): outstanding loans + this amount may not exceed `loan_max_salaries` salaries."""
    limit = effective_salary(conn, staff_row, as_of_month, cfg) * float(cfg.get("loan_max_salaries", 2))
    outstanding = loan_outstanding(conn, staff_row["emp_id"], as_of_month)
    return limit, outstanding, outstanding + amount > limit

def approve_loan_application(conn, app_row, amount, user, now):
    """Create the advances_loans record: recovered from the month after approval, in equal instalments."""
    import math
    ref = f"LA{app_row['id']:04d}"
    approved_on = date.today()
    instalment = amount if app_row["loan_type"] == "Advance" else math.ceil(amount / app_row["instalments"])
    conn.execute("""INSERT INTO advances_loans (loan_ref, emp_id, loan_type, date_given, amount, monthly_instalment, first_recovery_month,
                                               purpose, no_of_instalments, recommended_by, approved_by, approval_date, remarks, disbursed)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                 (ref, app_row["emp_id"], app_row["loan_type"], fmt_date(approved_on), amount, instalment,
                  fmt_date(add_months(first_of_month(approved_on), 1)), app_row["purpose"], app_row["instalments"],
                  app_row["reviewed_by"], user, fmt_date(approved_on), f"Applied online (application #{app_row['id']})"
                  + (" — approved as exception to loan policy" if app_row["exceeds_policy"] else "")))
    return ref, instalment

OWN_VEHICLE = "Own vehicle"  # paid kms x the petrol-linked rate
RIDE_SERVICES = ("InDrive", "Yango", "Cab / Careem / Uber", "Auto rickshaw", "Bike ride service")  # paid at the actual fare
TRAVEL_MODES = (OWN_VEHICLE,) + RIDE_SERVICES

FUEL_PRODUCT = "PREMIER EURO 5"

def parse_pso_prices(html):
    """[(YYYY-MM-DD, price)] for Premier Euro 5 from a PSO 'POL archives' page: blocks headed
    'Effective From: November 18, 2026', each with a 'PREMIER EURO 5 | Rs.325/Ltr' row."""
    import re
    out = []
    blocks = re.split(r"Effective From:\s*", html)[1:]
    for block in blocks:
        m_date = re.match(r"([A-Za-z]+ \d{1,2}, \d{4})", block)
        m_price = re.search(re.escape(FUEL_PRODUCT) + r"\s*</td>\s*<td>\s*Rs\.?\s*([\d,]+(?:\.\d+)?)\s*/\s*Ltr", block, re.I)
        if not m_date or not m_price:
            continue
        try:
            d = datetime.strptime(m_date.group(1), "%B %d, %Y").date()
        except ValueError:
            continue
        price = float(m_price.group(1).replace(",", ""))
        if price > 0:
            out.append((fmt_date(d), price))
    return out

def fetch_fuel_prices(conn, pages=1, url=None):
    """Read the latest Premier Euro 5 prices from the PSO website into fuel_prices (manual entries are kept).
    Returns the number of new dates added; raises on a network / format problem."""
    import urllib.request
    base = url or get_settings_dict(conn).get("fuel_price_url", "https://psopk.com/fuel-prices/pol/archives?page=1")
    added = found = 0
    now = datetime.now().isoformat(timespec="seconds")
    for page in range(1, pages + 1):
        page_url = base.split("?")[0] + f"?page={page}"
        req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0 (payroll travel-rate update)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", "replace")
        rows = parse_pso_prices(html)
        found += len(rows)
        for d, price in rows:
            cur = conn.execute("INSERT OR IGNORE INTO fuel_prices (effective_date, price, source, recorded_by, recorded_at) VALUES (?,?,?,?,?)",
                               (d, price, "PSO website", "system", now))
            added += cur.rowcount
    if not found:
        raise ValueError(f"No '{FUEL_PRODUCT}' prices found on the PSO page (the page layout may have changed)")
    return added

def petrol_price_on(conn, on_date):
    """(effective date, price) of the Premier Euro 5 price in force on a date, or (None, None)."""
    row = conn.execute("SELECT effective_date, price FROM fuel_prices WHERE effective_date <= ? ORDER BY effective_date DESC LIMIT 1",
                       (fmt_date(on_date) if isinstance(on_date, date) else on_date,)).fetchone()
    return (row["effective_date"], row["price"]) if row else (None, None)

def travel_rate_on(conn, on_date, cfg=None):
    """Rs. per km for a claim dated `on_date`: petrol price that day x base Rs./km ÷ base petrol price
    (Rs. 17/km at Rs. 400/litre). Falls back to travel_rate_per_km when no price is on file."""
    cfg = cfg or get_settings_dict(conn)
    _, price = petrol_price_on(conn, on_date)
    uplift = rate_uplift_on(conn, on_date)
    return km_rate_from_price(price, cfg, uplift["pct"] if uplift else 0)

def rate_uplift_on(conn, on_date):
    """The uplift in force on a date (row with pct, reason, set_by), or None."""
    return conn.execute("SELECT * FROM rate_uplifts WHERE effective_from <= ? ORDER BY effective_from DESC, id DESC LIMIT 1",
                        (fmt_date(on_date) if isinstance(on_date, date) else on_date,)).fetchone()

def km_rate_from_price(price, cfg, uplift_pct=0):
    """price x base Rs./km ÷ base price (or the fallback rate when no price), plus any uplift %, to the paisa,
    rounded half up (exact decimal arithmetic)."""
    from decimal import Decimal, ROUND_HALF_UP
    if price is None:
        rate = Decimal(str(cfg.get("travel_rate_per_km", 17)))
    else:
        rate = (Decimal(str(price)) * Decimal(str(cfg.get("travel_rate_base_km", 17)))
                / Decimal(str(cfg.get("travel_rate_base_price", 400))))
    rate *= 1 + Decimal(str(uplift_pct or 0)) / 100
    return float(rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def fuel_price_updater(interval_hours=6):
    """Background thread: keep the PSO petrol prices current (first run also fills the last few months)."""
    import threading, time
    def run():
        while True:
            conn = get_conn()
            try:
                empty = conn.execute("SELECT COUNT(*) FROM fuel_prices WHERE source = 'PSO website'").fetchone()[0] == 0
                added = fetch_fuel_prices(conn, pages=6 if empty else 1)
                status = f"OK — {added} new price(s)"
            except Exception as e:  # offline or the site changed: keep the last prices, show the problem on the Travel tab
                status = f"Failed — {type(e).__name__}: {str(e)[:150]}"
            conn.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('fuel_price_last_check', ?, 'Last PSO price check')",
                         (f"{datetime.now().isoformat(timespec='minutes')} {status}",))
            conn.commit()
            conn.close()
            time.sleep(interval_hours * 3600)
    threading.Thread(target=run, daemon=True).start()

def next_open_month(conn, payroll_month):
    """The month itself if still Draft, otherwise the first later month not yet prepared for sign-off."""
    pm = parse_date(payroll_month)
    while is_month_locked(conn, fmt_date(pm)):
        pm = add_months(pm, 1)
    return fmt_date(pm)

def review_travel(conn, claim, decision, chargeable, client_name, note, user, own_emp, role=None):
    """Travel claims: Submitted -> Reviewed (paid in payroll; marked chargeable to the client or not) or Rejected.
    Nobody reviews their own claim (nor the reviewer those of staff whose portal he operates: the approver does).
    A claim reviewed after its month was signed off is paid in the next open month."""
    if (own_emp and claim["emp_id"] == own_emp) or (role == ROLE_REVIEWER and claim["emp_id"] in approver_only_emps(conn)):
        raise ChangeError("own_claim")
    if claim["status"] != "Submitted":
        raise ChangeError("claim_wrong_stage")
    now = datetime.now().isoformat(timespec="seconds")
    if decision == "Reviewed":
        client = client_name.strip()[:120] or (claim["client_name"] or "")
        if chargeable and not client:
            raise ChangeError("claim_need_client")
        conn.execute("""UPDATE travel_claims SET status = 'Reviewed', chargeable = ?, client_name = ?, reviewed_by = ?, reviewed_at = ?,
                        payroll_month = ? WHERE id = ?""",
                     (1 if chargeable else 0, client, user, now, next_open_month(conn, claim["payroll_month"]), claim["id"]))
        return "claim_reviewed"
    if decision == "Rejected":
        if not note.strip():
            raise ChangeError("claim_need_note")
        conn.execute("UPDATE travel_claims SET status = 'Rejected', review_note = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                     (note.strip()[:300], user, now, claim["id"]))
        return "claim_rejected"
    raise ChangeError("bad_input")

def chargeable_to_invoice(conn):
    """Chargeable claims paid in an approved payroll and not yet included in a client invoice, by client and month."""
    return conn.execute("""
        SELECT t.client_name, t.payroll_month, COUNT(*) AS n, SUM(t.total_amount) AS amount,
               GROUP_CONCAT(s.employee_name || ' (' || t.claim_date || ', Rs. ' || CAST(t.total_amount AS INTEGER) || ')', '; ') AS detail
        FROM travel_claims t JOIN staff s ON s.emp_id = t.emp_id
        JOIN payroll_runs r ON r.payroll_month = t.payroll_month AND r.status = 'Approved'
        WHERE t.status = 'Reviewed' AND t.chargeable = 1 AND t.invoiced = 0
        GROUP BY t.client_name, t.payroll_month ORDER BY t.payroll_month, t.client_name
    """).fetchall()

FLASH_MESSAGES = {  # key -> (style, text)
    "submitted": ("ok", "Submitted for approval. Nothing changes until the approver approves it (see the Approvals tab)."),
    "applied": ("ok", "Change applied and recorded in the approval log."),
    "saved": ("ok", "Saved."),
    "req_approved": ("ok", "Request approved and applied."),
    "req_rejected": ("ok", "Request rejected. Nothing was changed."),
    "month_prepared": ("ok", "Payroll marked as prepared and locked. It now goes to the reviewer."),
    "month_reviewed": ("ok", "Payroll marked as reviewed. It now goes to the approver."),
    "month_approved": ("ok", "Payroll approved. Bank files can now be exported."),
    "month_sent_back": ("warn", "Payroll sent back to Draft. Figures can change again; it must be prepared, reviewed and approved again."),
    "wrong_stage": ("warn", "That step isn't available at the payroll's current stage (Draft → Prepared → Reviewed → Approved)."),
    "locked": ("warn", "That payroll month has been prepared for sign-off and is locked. The reviewer or approver must send it back to Draft before anything can change."),
    "export_locked": ("warn", "Bank files can only be exported after the approver has approved this month's payroll."),
    "not_allowed": ("warn", "Your role doesn't allow that."),
    "own_leave": ("warn", "You can't approve or reject your own leave."),
    "leave_recommended": ("ok", "Leave recommended. It now goes to the approver."),
    "leave_approved": ("ok", "Leave approved."),
    "leave_rejected": ("ok", "Leave rejected; it no longer counts towards leave or deductions."),
    "leave_cancelled": ("ok", "Absence record cancelled; the salary is no longer withheld for it."),
    "leaves_bulk": ("ok", "All eligible leaves updated."),
    "wrong_leave_stage": ("warn", "That leave can't take this step. Leaves go Pending → Recommended (reviewer) → Approved (approver)."),
    "no_absence_entry": ("warn", "Only the preparer or approver can record an unapproved absence."),
    "rate_updated": ("ok", "Travel rate formula updated. It applies to travel claims entered from now on."),
    "leave_file_previewed": ("ok", "Leave history read. Check the preview, then apply it (nothing has changed yet)."),
    "leave_file_applied": ("ok", "Leave history applied: opening balances and leave records updated, recorded as approved."),
    "leave_file_unreadable": ("warn", "Couldn't read the leave file. Use the Leave History template (Excel .xlsx) and keep its headings."),
    "noc_needs_residual": ("warn", "A NOC student needs the residual leaves and the date the residual period ends."),
    "security_released": ("ok", "Held pay / security released into this month's payroll."),
    "security_forfeited": ("warn", "Held pay / security forfeited and recorded in the approval log."),
    "security_need_handover": ("warn", "Confirm that proper notice was served and handing / taking over is complete before releasing."),
    "security_need_note": ("warn", "Write the reason for forfeiting the held amount."),
    "security_invalid": ("warn", "Held pay can only be settled once, for someone who has left or has a leaving date, in a Draft month not before their leaving month."),
    "attendance_uploaded": ("ok", "Attendance uploaded. Check the absences not covered by a leave below, then continue with the payroll."),
    "attendance_partly": ("warn", "Attendance uploaded, but some people in the report couldn't be matched to staff. Match them below."),
    "attendance_unreadable": ("warn", "Couldn't read the attendance report. Upload the PDF 'Monthly Performance Report' from the attendance software."),
    "attendance_mapped": ("ok", "Attendance code matched to the staff member and their attendance saved."),
    "absences_filed": ("ok", "Absent days filed as leave applications (Pending: reviewer recommends, approver approves)."),
    "absences_recorded": ("warn", "Absent days recorded as unapproved absence: that month's salary is withheld until the approver releases it."),
    "need_attendance": ("warn", "Upload this month's attendance report first (Attendance tab) — the payroll starts with the attendance."),
    "sheet_previewed": ("ok", "Final payroll sheet read. Check the preview below, then apply it (nothing has changed yet)."),
    "sheet_applied": ("ok", "Final payroll sheet applied: this month's payroll now shows the sheet's figures and is approved and locked."),
    "sheet_discarded": ("ok", "Preview discarded. Nothing was changed."),
    "sheet_unreadable": ("warn", "Couldn't read the payroll sheet. Upload an Excel (.xlsx) or CSV file with a heading row that has an Emp ID or Name column and a Net Pay column."),
    "imported_month": ("warn", "This month's figures come from the uploaded final sheet, so it can't be sent back for recalculation. Upload a corrected sheet instead."),
    "uplift_saved": ("ok", "Travel rate uplift saved and recorded in the approval log. It applies to claims dated from its start date."),
    "uplift_too_high": ("warn", "The reviewer can increase the travel rate by at most 10% above the formula rate (0% returns to the formula)."),
    "uplift_need_reason": ("warn", "Write the reason for the increase (at least a short sentence)."),
    "fuel_fetched": ("ok", "Petrol prices updated from the PSO website."),
    "fuel_fetch_failed": ("warn", "Couldn't read the PSO website (no internet, or the page changed). The last prices on file are still used; you can enter today's price by hand."),
    "fuel_price_saved": ("ok", "Petrol price saved. Claims dated from that day use the new rate."),
    "fuel_price_invalid": ("warn", "Enter the date the price took effect (not in the future) and the price per litre."),
    "claim_future": ("warn", "The travel date can't be in the future."),
    "claim_need_fare": ("warn", "For InDrive, Yango, cab, auto or bike service, enter the actual fare paid."),
    "rate_invalid": ("warn", "Enter a per-km rate greater than 0."),
    "salary_released": ("ok", "Withheld salary released into this month's payroll."),
    "late_released": ("ok", "Late-arrival hold released into this month's payroll and recorded in the approval log."),
    "timing_saved": ("ok", "Office timing saved. Late arrivals and absences now follow it; it is recorded in the approval log."),
    "timing_removed": ("ok", "Office timing removed. The firm's timing applies again for those dates."),
    "timing_invalid": ("warn", "Check the timing: enter the staff member, a start date, a start time before the end time, at least one working day and the reason."),
    "timing_own": ("warn", "You can't set your own office timing. The approver sets it."),
    "hours_saved": ("ok", "Weekly hours saved for this month. The stipend is worked out on these hours and the change is in the approval log."),
    "hours_invalid": ("warn", "Choose the staff member and enter the weekly hours (more than 0)."),
    "hours_own": ("warn", "You can't set your own working hours. The approver sets them."),
    "release_invalid": ("warn", "Withheld salary can only be released from an approved earlier month into a later Draft month, and only once."),
    "no_emp_link": ("warn", "Your login isn't linked to a staff member. Ask the approver to link it on the Users tab."),
    "need_emp": ("warn", "Staff logins must be linked to an existing Emp ID."),
    "pw_changed": ("ok", "Password changed."),
    "pw_must_change": ("warn", "Welcome. Please change your starting password before continuing: enter the current password and choose a new one of your own."),
    "pw_invalid": ("warn", f"Current password is wrong, or the new password is shorter than {MIN_PASSWORD_LEN} characters or doesn't match."),
    "emp_missing": ("warn", "No staff member has that Emp ID. New staff can only be added with 'Add New Staff Member', and need approval."),
    "emp_exists": ("warn", "That Emp ID already exists."),
    "not_employee": ("warn", "That person is EOBI-only (not an employee), so no leave or travel can be entered for them."),
    "nothing_changed": ("warn", "Nothing to change: every field was blank or the same as now."),
    "bad_input": ("warn", "Some required fields were missing or invalid."),
    "user_added": ("ok", "User added."),
    "user_deleted": ("ok", "User removed."),
    "user_exists": ("warn", "That username already exists."),
    "user_invalid": ("warn", f"Enter a username and a password of at least {MIN_PASSWORD_LEN} characters."),
    "cannot_delete_self": ("warn", "You can't remove your own account."),
    "ts_saved": ("ok", "Timesheet saved as draft. Submit it when the month is complete."),
    "ts_submitted": ("ok", "Timesheet submitted. It now goes to the reviewer."),
    "ts_reviewed": ("ok", "Timesheet reviewed. It now goes to the approver."),
    "ts_approved": ("ok", "Timesheet approved. The student's salary for that month is released."),
    "ts_returned": ("warn", "Timesheet returned to the student for correction."),
    "ts_not_icap": ("warn", "No timesheet is required for this month: timesheets are for ICAP students, and for other staff only from the month the reviewer asks."),
    "ts_access_on": ("ok", "Timesheets enabled for this staff member. From that month the same rules apply: salary is held until the timesheet is approved."),
    "ts_access_off": ("ok", "Timesheets no longer required for this staff member."),
    "ts_access_invalid": ("warn", "Choose an active non-ICAP staff member (ICAP students always submit timesheets) and a start month."),
    "ts_locked": ("warn", "This timesheet has been submitted and can't be edited unless it is returned."),
    "ts_wrong_stage": ("warn", "That step isn't available. Timesheets go Submitted → Reviewed (reviewer) → Approved (approver)."),
    "ts_own": ("warn", "You can't review or approve your own timesheet."),
    "ts_need_scan": ("warn", "Upload the scanned timesheet (PDF, JPG or PNG) and enter the total hours before submitting."),
    "ts_bad_file": ("warn", "The file must be a PDF, JPG or PNG of at most 10 MB."),
    "ts_need_entries": ("warn", "Fill in the days worked (client, nature of work and hours) before submitting."),
    "ts_bad_hours": ("warn", "Hours per day must be between 0 and 24."),
    "ts_need_note": ("warn", "Write a note telling the student what to correct."),
    "ts_future": ("warn", "A month's timesheet can't be submitted before the month has started."),
    "ts_bad_code": ("warn", "Choose Line of Service, Head of Account and Work Performed from the lists. Ask the reviewer to add a missing code."),
    "claim_submitted": ("ok", "Travel claim submitted to the reviewer. It is paid in payroll once reviewed."),
    "claim_reviewed": ("ok", "Travel claim reviewed. It will be paid in payroll."),
    "claim_rejected": ("ok", "Travel claim rejected; it will not be paid."),
    "claim_withdrawn": ("ok", "Travel claim withdrawn."),
    "own_claim": ("warn", "You can't review your own travel claim."),
    "claim_wrong_stage": ("warn", "That claim has already been reviewed or rejected."),
    "claim_need_client": ("warn", "Enter the client the expense is chargeable to."),
    "claim_need_note": ("warn", "Write a note saying why the claim is rejected."),
    "invoiced": ("ok", "Marked as included in the client invoice. It no longer appears in the reminder."),
    "invoice_invalid": ("warn", "Nothing to mark: those chargeable claims are not outstanding."),
    "doc_missing": ("warn", "Attach every mandatory document (PDF, JPG or PNG) and enter the CNIC expiry date. Mandatory documents are listed on the form."),
    "doc_bad_file": ("warn", "Documents must be PDF, JPG or PNG files of at most 10 MB each."),
    "doc_saved": ("ok", "Document saved."),
    "doc_type_saved": ("ok", "Document requirements updated. They apply to new staff from now on."),
    "doc_type_invalid": ("warn", "Enter the document name and who it applies to."),
    "doc_type_exists": ("warn", "A document type with that name already exists."),
    "delegate_saved": ("ok", "Portal operator saved. That user can now switch into this staff member's portal."),
    "delegate_removed": ("ok", "Portal operator removed."),
    "delegate_invalid": ("warn", "Choose a staff member who has a Staff login and an operator who is an approver, reviewer or preparer."),
    "loan_files_done": ("ok", "Loan bank files generated (separate from salaries). Upload them to the bank; the loans are marked as paid."),
    "loan_files_partly": ("warn", "Loan bank files generated, but some loans were left out because the staff member has no bank account or IBFT bank code. Fix the bank details and generate again."),
    "loan_files_none": ("warn", "No approved loans are waiting to be paid (or none has bank details yet)."),
    "loan_applied": ("ok", "Loan / advance application submitted. The reviewer reviews it and the approver decides."),
    "loan_recommended": ("ok", "Application recommended. It now goes to the approver, who has been notified."),
    "loan_approved": ("ok", "Loan / advance approved. Recovery starts from next month's payroll."),
    "loan_rejected": ("ok", "Application rejected."),
    "loan_withdrawn": ("ok", "Application withdrawn."),
    "loan_invalid": ("warn", "Enter the amount and purpose; a loan needs 1 to 20 monthly instalments."),
    "loan_instalments": ("warn", "A loan can be repaid over at most 20 monthly instalments (policy)."),
    "loan_need_exception": ("warn", "This application exceeds the loan policy (2 salaries). To recommend it, tick 'Recommend as exception' and write the justification."),
    "loan_wrong_stage": ("warn", "That application can't take this step. Applications go Applied → Recommended (reviewer) → Approved (approver)."),
    "loan_own": ("warn", "You can't review or approve your own loan application."),
    "loan_bad_amount": ("warn", "The approved amount must be more than 0 and not more than the amount applied for."),
    "ts_bad_day": ("warn", "'Public Holiday' can only be used on a gazetted holiday (ask the reviewer to add it), and 'Weekend' only on a Saturday or Sunday."),
    "holiday_saved": ("ok", "Gazetted holiday saved. It now shows as a holiday on timesheets."),
    "holiday_deleted": ("ok", "Holiday removed."),
    "holiday_notice_sent": ("ok", "Holiday list updated and emailed to all staff. See the Email Log on the Email tab."),
    "holiday_notice_partly": ("warn", "Holiday list updated, but the email did not reach everyone. See the Email Log on the Email tab."),
    "holiday_notice_failed": ("warn", "Holiday list updated, but the email could not be sent. Check your account on the Email tab, then use Email Holiday List Again."),
    "holiday_notice_no_account": ("warn", "Holiday list updated, but not emailed: set up your sending email account on the Email tab, then use Email Holiday List Again."),
    "holiday_notice_no_staff": ("warn", "Holiday list updated, but no staff email addresses are on record."),
    "holiday_invalid": ("warn", "Enter the holiday date and its name."),
    "holidays_emailed": ("ok", "Holiday list emailed to staff. See the Email Log on the Email tab."),
    "holidays_email_partly": ("warn", "Some holiday emails could not be sent. See the Email Log on the Email tab."),
    "holidays_none": ("warn", "No upcoming holidays to email, or no staff email addresses yet."),
    "credit_added": ("ok", "Leave credit recommended. It now goes to the approver, who has been notified."),
    "credit_applied": ("ok", "Leave credit granted and recorded in the approval log."),
    "credit_approved": ("ok", "Leave credit approved; it is added to the staff member's leave."),
    "credit_rejected": ("ok", "Leave credit rejected."),
    "credit_reason": ("warn", "Please choose a valid reason for the credit leave."),
    "credit_date": ("warn", "The date must be a past gazetted holiday, or a past Saturday / Sunday that is the person's day off."),
    "credit_duplicate": ("warn", "A leave credit already exists for that person and date."),
    "credit_own": ("warn", "You can't decide a credit leave for yourself or for a portal you operate; the approver decides it."),
    "credit_submitted": ("ok", "Credit leave application submitted. It now goes to the reviewer."),
    "credit_submitted_late": ("warn", "Submitted after the 7-day limit. It moves forward only if the reviewer condones the delay."),
    "credit_recommended": ("ok", "Credit leave recommended. It now goes to the approver."),
    "credit_condoned": ("ok", "Delay condoned and credit leave recommended to the approver."),
    "credit_need_note": ("warn", "Please write the reason (the work done, or why the delay is condoned or the application rejected)."),
    "credit_full_day": ("warn", "Credit leave is only for a complete working day. Please tick the confirmation."),
    "credit_wrong_stage": ("warn", "That leave credit has already been decided."),
    "approver_email_saved": ("ok", "Approver's notification email saved."),
    "emails_uploaded": ("ok", "Email list uploaded. See the upload result below."),
    "email_list_invalid": ("warn", "Upload a CSV or Excel (.xlsx) list with an 'Email' column and an 'Emp ID', 'CNIC' or 'Name' column."),
    "email_saved": ("ok", "Email address saved."),
    "email_invalid": ("warn", "Enter a valid email address."),
    "mail_account_saved": ("ok", "Your sending email account is saved. Send yourself a test email to check it."),
    "mail_account_invalid": ("warn", "Enter your email address, the SMTP server and port, and choose the security."),
    "mail_no_account": ("warn", "Set up your sending email account first (My Sending Email Account below)."),
    "mail_test_sent": ("ok", "Test email sent to your own address. Check your inbox."),
    "mail_failed": ("warn", "The email could not be sent. See the Email Log below for the reason."),
    "logins_sent": ("ok", "Login details sent. See the Email Log below for each person."),
    "logins_partly": ("warn", "Some login emails could not be sent. See the Email Log below."),
    "logins_none": ("warn", "Nobody to send to: login details go only to staff with an email address whose starting password hasn't been changed yet."),
    "app_url_saved": ("ok", "Payroll address saved. It is used in login emails."),
    "code_saved": ("ok", "Code saved. It is now in the timesheet drop-down list."),
    "code_invalid": ("warn", "Enter a code (letters, digits, - or /, up to 12 characters) and choose its list."),
}

# ==============================================================================
# 7b. ICAP STUDENT TIMESHEETS (monthly daily diary)
# ==============================================================================
# Each ICAP student keeps a monthly daily diary. Chain: Draft -> Submitted (student) -> Reviewed (Reviewer) -> Approved
# (Approver); the reviewer or approver can return it for correction. The student's salary for the month is held until
# its timesheet is approved. Months before `timesheet_online_from` are paper sheets: the scan is uploaded and totals typed in.

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
SCAN_TYPES = ((b"%PDF", "application/pdf", "pdf"), (b"\x89PNG", "image/png", "png"), (b"\xff\xd8\xff", "image/jpeg", "jpg"))

def month_holidays(conn, pm_str):
    """{YYYY-MM-DD: name} of the gazetted holidays in the calendar month."""
    days = month_days(pm_str)
    return {r["holiday_date"]: r["name"] for r in conn.execute(
        "SELECT holiday_date, name FROM holidays WHERE holiday_date BETWEEN ? AND ?", (fmt_date(days[0]), fmt_date(days[-1])))}

def default_day_type(d, holidays):
    if fmt_date(d) in holidays:
        return "Public Holiday"
    return "Weekend" if d.weekday() >= 5 else "Work"

def check_credit(conn, staff, work_date, reason):
    """Credits, for all staff including ICAP / NOC students: a gazetted holiday, or a Saturday / Sunday that is the person's day off.
    One credit per person per date."""
    if reason not in CREDIT_REASONS:
        raise ChangeError("credit_reason")
    d = parse_date(work_date)
    if d is None or d > date.today():
        raise ChangeError("credit_date")
    is_holiday = conn.execute("SELECT 1 FROM holidays WHERE holiday_date = ?", (fmt_date(d),)).fetchone() is not None
    timing = timing_on(staff_timing_rows(conn, staff["emp_id"]), fmt_date(d), get_settings_dict(conn))
    day_off = d.weekday() >= 5 and d.weekday() in timing["off"]  # a Saturday someone normally works earns no credit
    if (reason == "weekend_called" and not day_off) or (reason != "weekend_called" and not is_holiday):
        raise ChangeError("credit_date")
    if conn.execute("SELECT 1 FROM leave_credits WHERE emp_id = ? AND work_date = ? AND status != 'Rejected'",
                    (staff["emp_id"], fmt_date(d))).fetchone():
        raise ChangeError("credit_duplicate")
    return fmt_date(d)

def credit_is_late(work_date, cfg):
    return (date.today() - parse_date(work_date)).days > int(float(cfg.get("credit_apply_days", 7)))

def credit_review_form(c, ym, ok_label):
    """Recommend / condone / reject buttons for a credit leave application."""
    hint = "Reason for condoning or rejecting" if c["status"] == "Late" else "Reason (if rejecting)"
    return (f"<form method='POST' action='/review_credit?ym={ym}' style='display:flex; gap:6px; flex-wrap:wrap; align-items:center;'>"
            f"<input type='hidden' name='id' value='{c['id']}'><input name='note' size='22' placeholder='{hint}'>"
            f"<button class='btn btn-ok' type='submit' name='decision' value='Recommended'>{ok_label}</button>"
            "<button class='btn btn-danger' type='submit' name='decision' value='Rejected'>Reject</button></form>")

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

def month_days(pm_str):
    pm = parse_date(pm_str)
    return [date(pm.year, pm.month, d) for d in range(1, calendar.monthrange(pm.year, pm.month)[1] + 1)]

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
            fields[col] = float(data.get(col) or 0)
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
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            stored = f"timesheet_{emp_id}_{pm_str[:7]}_{secrets.token_hex(8)}.{kind[2]}"
            with open(os.path.join(UPLOAD_DIR, stored), "wb") as f:
                f.write(content)
            old = ts["scan_file"]
            if old and os.path.exists(os.path.join(UPLOAD_DIR, old)):
                os.remove(os.path.join(UPLOAD_DIR, old))
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
            hours = float(data.get(f"hours_{k}") or 0) if day_type == "Work" else 0.0
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

def parse_multipart(content_type, body):
    """multipart/form-data -> (fields, files) where files[name] = (filename, content_type, bytes)."""
    from email import policy
    from email.parser import BytesParser
    msg = BytesParser(policy=policy.HTTP).parsebytes(b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    fields, files = {}, {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[name] = (filename, part.get_content_type(), payload)
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files

# ==============================================================================
# 7b-2. STAFF DOCUMENTS (CNIC, qualification, CV, ...)
# ==============================================================================

MAX_FORM_BYTES = 45 * 1024 * 1024  # a form may carry several documents of up to MAX_UPLOAD_BYTES each

def doc_types_for(conn, staff_type, mandatory_only=False):
    """Active document types that apply to this staff type (EOBI-only people are not employees: none)."""
    if staff_type == EOBI_ONLY_TYPE:
        return []
    also = "ICAP Article" if staff_type == NOC_TYPE else staff_type or ""  # NOC students need what ICAP students need
    return conn.execute(f"""SELECT * FROM doc_types WHERE active = 1 AND (applies_to = 'All' OR applies_to = ? OR applies_to = ?)
                            {'AND mandatory = 1' if mandatory_only else ''} ORDER BY mandatory DESC, name""", (staff_type or "", also)).fetchall()

def held_balance(conn, staff_row, pm_str):
    """Pay still held for a person up to and including this payroll month (held pay or security), less any settlement."""
    h = conn.execute("""SELECT COALESCE(SUM(salary_withheld_probation), 0) w, COALESCE(SUM(probation_salary_released), 0) r
                        FROM payroll_lines WHERE emp_id = ? AND payroll_month <= ?""", (staff_row["emp_id"], pm_str)).fetchone()
    settled = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM security_settlements WHERE emp_id = ?", (staff_row["emp_id"],)).fetchone()[0]
    return float(staff_row["opening_withheld_probation"] or 0) + float(h["w"]) - float(h["r"]) - float(settled)

def missing_documents(conn, emp_id, staff_type):
    """Names of mandatory documents not yet on file for this staff member."""
    have = {r["doc_type"] for r in conn.execute("SELECT doc_type FROM staff_documents WHERE emp_id = ? AND replaced = 0", (emp_id,))}
    return [t["name"] for t in doc_types_for(conn, staff_type, mandatory_only=True) if t["code"] not in have]

def check_document(upload):
    """(original name, content, mime, extension) for a PDF / JPG / PNG of at most 10 MB, else ChangeError."""
    name, _, content = upload
    kind = next((k for k in SCAN_TYPES if content.startswith(k[0])), None)
    if kind is None or len(content) > MAX_UPLOAD_BYTES:
        raise ChangeError("doc_bad_file")
    return os.path.basename(name.replace("\\", "/"))[:120], content, kind[1], kind[2]

def save_document(conn, emp_id, doc_type, upload, expiry, user, request_id=None):
    """Store one staff document; an older copy of the same type is kept but marked replaced. A CNIC's expiry date
    is copied to the staff record so the system can warn when it expires."""
    name, content, mime, ext = check_document(upload)
    folder = os.path.join(UPLOAD_DIR, "documents")
    os.makedirs(folder, exist_ok=True)
    stored = f"{emp_id}_{doc_type}_{secrets.token_hex(8)}.{ext}"
    with open(os.path.join(folder, stored), "wb") as f:
        f.write(content)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE staff_documents SET replaced = 1 WHERE emp_id = ? AND doc_type = ? AND replaced = 0", (emp_id, doc_type))
    conn.execute("""INSERT INTO staff_documents (emp_id, doc_type, file, original_name, mime, expiry_date, uploaded_by, uploaded_at, change_request_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""", (emp_id, doc_type, stored, name, mime, expiry, user, now, request_id))
    if doc_type == "cnic" and expiry:
        conn.execute("UPDATE staff SET cnic_expiry = ? WHERE emp_id = ?", (expiry, emp_id))

def delete_request_documents(conn, request_id):
    """A rejected add-staff request: remove the documents that came with it."""
    folder = os.path.join(UPLOAD_DIR, "documents")
    for d in conn.execute("SELECT file FROM staff_documents WHERE change_request_id = ?", (request_id,)).fetchall():
        try:
            os.remove(os.path.join(folder, d["file"]))
        except OSError:
            pass
    conn.execute("DELETE FROM staff_documents WHERE change_request_id = ?", (request_id,))

# ==============================================================================
# 7b-2b. ATTENDANCE (report from the attendance machine software)
# ==============================================================================
# The preparer starts each month's payroll by uploading the attendance software's "Monthly Performance Report" (PDF).
# Each person's daily status is stored; absences not covered by a leave (or holiday) are listed so the preparer can
# file them as leave or record them as unapproved absences. The month can't be marked prepared before attendance is in.

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
            conn.execute("INSERT OR REPLACE INTO attendance (emp_id, att_date, status, upload_id, arrival_time) VALUES (?,?,?,?,?)",
                         (emp, d, st, upload_id, p.get("arrivals", {}).get(d)))
    return unmatched

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
    for emp, e in out.items():
        leaves = conn.execute("""SELECT date_from, date_to FROM leave_register WHERE emp_id = ? AND status NOT IN ('Rejected', 'Cancelled')
                                 AND date_to >= ? AND date_from <= ?""", (emp, first, last)).fetchall()
        def covered(d):
            return d in holidays or any(l["date_from"] <= d <= l["date_to"] for l in leaves)
        e["covered"] = [d for d in e["absent"] if covered(d)]
        e["uncovered"] = [d for d in e["absent"] if not covered(d)]
    return out

# ==============================================================================
# 7b-2c. LEAVE HISTORY IMPORT (opening balances as at 31 Dec 2025 + every leave from 1 Jan 2026)
# ==============================================================================

LEAVE_HISTORY_FROM = "2026-01-01"  # opening balances are the leaves taken before this date

def leave_type_from_text(text):
    t = (text or "").strip().lower()
    if "urgent" in t:
        if any(w in t for w in ("outside", "abroad", "foreign")):
            return URGENT_WORK_ABROAD
        if any(w in t for w in ("within", "inside", "pakistan")):
            return URGENT_WORK_PK
        return None  # say where: within or outside Pakistan
    for words, ltype in ((("study", "exam"), STUDY_LEAVE_TYPE), (("sick", "medical"), "Sick"), (("absen", "unapproved"), ABSENCE_LEAVE_TYPE)):
        if any(w in t for w in words):
            return ltype
    return None

def sheet_date(v):
    if isinstance(v, (datetime, date)):
        return v.date() if isinstance(v, datetime) else v
    s = str(v or "").strip()[:10]
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None

LEAVE_ROWS = 1500  # rows prepared on the Leaves sheet

def build_leave_template(conn):
    """Excel leave record: 'Opening Balance' (as at 1 Jan 2026, typed in by hand), 'Leaves' (one leave per row: employee
    and leave type from drop-downs, Emp ID and days filled in by formula), and a 'Summary' that keeps a running balance."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation
    head = (Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0F172A"))
    entry = PatternFill("solid", fgColor="FEF9C3")  # cells to fill in
    staff = conn.execute("""SELECT emp_id, employee_name, staff_type, leave_entitlement_override FROM staff
                            WHERE status = 'Active' AND COALESCE(staff_type, '') != ? ORDER BY emp_id""", (EOBI_ONLY_TYPE,)).fetchall()
    types = [t for t in LEAVE_TYPES if t != ABSENCE_LEAVE_TYPE]
    cfg = get_settings_dict(conn)
    wb = openpyxl.Workbook()

    ws = wb.active
    ws.title = "Opening Balance"
    ws.append([f"{FIRM_NAME} — Leave Record: opening balance as at 1 January 2026"])
    ws.append(["Type in the leave days each person had already taken before 1 January 2026 "
               "(ICAP students: total since the articleship began; Permanent / Internees: 0 — their 24 leaves restart each year)."])
    ws.append([])
    ws.append(["Emp ID", "Employee Name", "Staff Type", "Opening Balance (leaves taken before 1 Jan 2026)", "Of which Study Leave", "Remarks"])
    for c in ws[4]:
        c.font, c.fill = head
    for s in staff:
        ws.append([s["emp_id"], s["employee_name"], s["staff_type"], None, None, None])
        for col in "DEF":
            ws[f"{col}{ws.max_row}"].fill = entry
    for col, w in zip("ABCDEF", (9, 28, 14, 30, 20, 30)):
        ws.column_dimensions[col].width = w
    ws["A1"].font = Font(bold=True, size=13)

    lists = wb.create_sheet("Lists")  # drop-down sources (hidden)
    lists.append(["Employee Name", "Emp ID", "Leave Type"])
    for i in range(max(len(staff), len(types))):
        lists.append([staff[i]["employee_name"] if i < len(staff) else None, staff[i]["emp_id"] if i < len(staff) else None,
                      types[i] if i < len(types) else None])
    lists.sheet_state = "hidden"
    n_staff, n_types = len(staff) + 1, len(types) + 1

    lv = wb.create_sheet("Leaves")
    lv.append([f"{FIRM_NAME} — Leave Record from 1 January 2026 (enter each leave on its own row)"])
    lv.append(["Choose the Employee Name and Leave Type from the lists; Emp ID and Days fill in by themselves (type 0.5 in Days for half a day)."])
    lv.append(["Emp ID", "Employee Name", "Leave Type", "From", "To", "Days", "Reason"])
    for c in lv[3]:
        c.font, c.fill = head
    lv["A1"].font = Font(bold=True, size=13)
    name_dv = DataValidation(type="list", formula1=f"=Lists!$A$2:$A${n_staff}", allow_blank=True)
    type_dv = DataValidation(type="list", formula1=f"=Lists!$C$2:$C${n_types}", allow_blank=True)
    lv.add_data_validation(name_dv)
    lv.add_data_validation(type_dv)
    last = 3 + LEAVE_ROWS
    name_dv.add(f"B4:B{last}")
    type_dv.add(f"C4:C{last}")
    for r in range(4, last + 1):
        lv[f"A{r}"] = f'=IF(B{r}="","",IFERROR(VLOOKUP(B{r},Lists!$A$2:$B${n_staff},2,FALSE),""))'
        lv[f"F{r}"] = f'=IF(AND(ISNUMBER(D{r}),ISNUMBER(E{r})),E{r}-D{r}+1,"")'
        lv[f"D{r}"].number_format = lv[f"E{r}"].number_format = "dd/mm/yyyy"
        for col in "BCDEG":
            lv[f"{col}{r}"].fill = entry
    for col, w in zip("ABCDEFG", (9, 28, 30, 12, 12, 7, 36)):
        lv.column_dimensions[col].width = w
    lv.freeze_panes = "A4"

    sm = wb.create_sheet("Summary")  # running balance, worked out by formulas
    sm.append([f"{FIRM_NAME} — Leave balances (worked out from the other sheets)"])
    sm.append([])
    sm.append(["Emp ID", "Employee Name", "Staff Type", "Opening", "Taken 2026", "Of which Study (2026)",
               "Total Counted", "Entitlement", "Balance"])
    for c in sm[3]:
        c.font, c.fill = head
    sm["A1"].font = Font(bold=True, size=13)
    icap_total, annual = float(cfg.get("icap_articleship_leave_total", 130)), float(cfg.get("leaves_per_year_permanent", 24))
    for i, s in enumerate(staff):
        r, o = 4 + i, 5 + i
        sm.append([s["emp_id"], s["employee_name"], s["staff_type"]])
        sm[f"D{r}"] = f"='Opening Balance'!D{o}"
        sm[f"E{r}"] = f"=SUMIFS(Leaves!$F$4:$F${last},Leaves!$B$4:$B${last},B{r})"
        sm[f"F{r}"] = f'=SUMIFS(Leaves!$F$4:$F${last},Leaves!$B$4:$B${last},B{r},Leaves!$C$4:$C${last},"{STUDY_LEAVE_TYPE}")'
        icap = s["staff_type"] in ARTICLE_TYPES
        sm[f"G{r}"] = f"=D{r}+E{r}" if icap else f"=E{r}"
        sm[f"H{r}"] = (s["leave_entitlement_override"] if s["staff_type"] == NOC_TYPE and s["leave_entitlement_override"] is not None
                       else icap_total if icap else annual)
        sm[f"I{r}"] = f"=H{r}-G{r}"
    for col, w in zip("ABCDEFGHI", (9, 28, 14, 10, 12, 18, 13, 12, 10)):
        sm.column_dimensions[col].width = w
    sm.append([])
    sm.append([f"ICAP students: {icap_total:g} leaves over the whole articleship (opening + 2026). Permanent / Internees: {annual:g} a year. "
               f"Study leave and '{URGENT_WORK_ABROAD}' count towards the balance but are never deducted from salary."])

    notes = wb.create_sheet("Instructions")
    notes.column_dimensions["A"].width = 110
    for line in ("How to keep this leave record",
                 "1. Opening Balance: type in, for each person, the leave days taken before 1 January 2026 (and how many were study leave).",
                 "2. Leaves: one row per leave, from 1 January 2026 onward. Pick the Employee Name and the Leave Type from the drop-down lists:",
                 "   " + " | ".join(types),
                 "   Enter From and To (dd/mm/yyyy). Days is worked out (To − From + 1); overwrite it, e.g. 0.5 for half a day.",
                 "3. Summary shows each person's opening, 2026 leaves, study leave and balance — it updates by itself.",
                 "4. Do not rename the headings or the sheets.",
                 "5. To load it into the payroll: Leave Register → Upload Leave History → check the preview → Apply.",
                 "   The leaves are recorded as approved by the approver; the opening balance is used as at 1 January 2026."):
        notes.append([line])
    notes["A1"].font = Font(bold=True, size=12)
    wb.move_sheet("Instructions", offset=-(len(wb.sheetnames) - 1))  # first tab
    wb.active = 0
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

def read_leave_history(conn, content):
    """{opening: {emp: (days, study)}, leaves: [{emp_id, leave_type, date_from, date_to, days, reason}], problems: [..]}."""
    import io
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    staff = conn.execute("SELECT emp_id, employee_name FROM staff").fetchall()
    by_id = {s["emp_id"].upper(): s["emp_id"] for s in staff}
    by_name = {}
    for s in staff:
        by_name.setdefault(name_key(s["employee_name"]), []).append(s["emp_id"])
    def who(emp_cell, name_cell):
        e = by_id.get(str(emp_cell or "").strip().upper())
        if e:
            return e
        m = by_name.get(name_key(name_cell), [])
        return m[0] if len(m) == 1 else None
    out = {"opening": {}, "leaves": [], "problems": []}
    for ws in wb.worksheets:
        if ws.title.strip().lower().startswith(("instruction", "summary", "lists")):
            continue  # explanation, formula results and drop-down sources, not data
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        for h_at, row in enumerate(rows[:10]):
            heads = [" ".join(str(c or "").lower().split()) for c in row]
            if sum(1 for h in heads if h) < 3:
                continue  # titles / notes are single cells; the heading row has several
            col = lambda *words: next((i for i, h in enumerate(heads) if any(w in h for w in words)), None)
            c_emp, c_name = col("emp id", "emp. id", "emp code"), col("name")
            if c_emp is None and c_name is None:
                continue
            c_before, c_study = col("before", "opening"), col("of which study", "study leave")
            c_type, c_from, c_to, c_days, c_reason = col("type"), col("from"), col("to"), col("days"), col("reason", "remark")
            get = lambda r, i: r[i] if i is not None and i < len(r) else None
            if c_before is not None:  # opening balance sheet
                for r in rows[h_at + 1:]:
                    if get(r, c_emp) is None and get(r, c_name) is None:
                        continue
                    emp = who(get(r, c_emp), get(r, c_name))
                    days = _num(get(r, c_before))
                    if emp is None:
                        out["problems"].append(f"Opening Balance: '{get(r, c_emp) or get(r, c_name)}' is not a staff member")
                    elif days is not None:
                        out["opening"][emp] = (days, _num(get(r, c_study)) or 0)
            elif c_from is not None and c_type is not None:  # day-by-day leaves sheet
                for n, r in enumerate(rows[h_at + 1:], h_at + 2):
                    if all(get(r, i) in (None, "") for i in (c_emp, c_name, c_type, c_from)):
                        continue
                    emp = who(get(r, c_emp), get(r, c_name))
                    ltype = leave_type_from_text(get(r, c_type))
                    f, t = sheet_date(get(r, c_from)), sheet_date(get(r, c_to)) or sheet_date(get(r, c_from))
                    days = _num(get(r, c_days))
                    label = f"Leaves row {n}"
                    if emp is None:
                        out["problems"].append(f"{label}: '{get(r, c_emp) or get(r, c_name)}' is not a staff member")
                    elif ltype is None:
                        out["problems"].append(f"{label}: leave type '{get(r, c_type)}' not recognised")
                    elif f is None or t is None or t < f:
                        out["problems"].append(f"{label}: dates missing or To before From")
                    elif fmt_date(f) < LEAVE_HISTORY_FROM:
                        out["problems"].append(f"{label}: {fmt_date(f)} is before 1 Jan 2026 — include it in the opening balance instead")
                    else:
                        out["leaves"].append({"emp_id": emp, "leave_type": ltype, "date_from": fmt_date(f), "date_to": fmt_date(t),
                                              "days": days if days is not None else (t - f).days + 1,
                                              "reason": str(get(r, c_reason) or "").strip()[:200]})
            break
    if not out["opening"] and not out["leaves"]:
        raise ChangeError("leave_file_unreadable")
    return out

def apply_leave_history(conn, data, user, replace_existing):
    """Opening balances become 'as at 31 Dec 2025'; each leave is recorded as approved by the approver. With
    replace_existing, the staff's earlier 2026 leave records (except recorded absences) are replaced by the file's."""
    now = datetime.now().isoformat(timespec="seconds")
    cutoff = int(float(get_settings_dict(conn).get("cutoff_day", 25)))
    if data["opening"]:
        conn.execute("UPDATE settings SET value = '2025-12-31' WHERE key = 'opening_balances_as_at'")
        for emp, (days, _study) in data["opening"].items():
            conn.execute("UPDATE staff SET opening_leaves_cumulative = ?, opening_leaves_year = 0 WHERE emp_id = ?", (days, emp))
    if replace_existing:
        for emp in {l["emp_id"] for l in data["leaves"]}:
            conn.execute("DELETE FROM leave_register WHERE emp_id = ? AND date_from >= ? AND status != 'Recorded'", (emp, LEAVE_HISTORY_FROM))
    added = skipped = 0
    for l in data["leaves"]:
        if conn.execute("SELECT 1 FROM leave_register WHERE emp_id = ? AND leave_type = ? AND date_from = ? AND date_to = ?",
                        (l["emp_id"], l["leave_type"], l["date_from"], l["date_to"])).fetchone():
            skipped += 1
            continue
        absence = l["leave_type"] == ABSENCE_LEAVE_TYPE
        conn.execute("""INSERT INTO leave_register (application_date, emp_id, leave_type, date_from, date_to, no_of_days, reason, recommended_by,
                                                    approved_by, status, payroll_month, entered_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (l["date_from"], l["emp_id"], l["leave_type"], l["date_from"], l["date_to"], l["days"], l["reason"] or "Leave history upload",
                      None if absence else "Leave history upload", None if absence else user, "Recorded" if absence else "Approved",
                      fmt_date(get_payroll_month_for_event(parse_date(l["date_from"]), cutoff)), user))
        added += 1
    return added, skipped

# ==============================================================================
# 7b-3. FINAL PAYROLL SHEET IMPORT (Excel prepared outside the system)
# ==============================================================================
# The approver can upload a month's final payroll sheet. Its columns are recognised by their headings; after a preview
# the sheet's figures replace that month's payroll and the month is approved and locked. Later months use the system's
# own rules (the sheet's withheld / recovered amounts carry forward like any other month).

SHEET_FIELDS = (  # payroll field, heading keywords (checked in this order, most specific first)
    ("net_pay", ("net pay", "net salary", "net payable", "net")),
    ("gross_earned", ("gross",)),
    ("leave_deduction", ("leave ded", "leave deduction")),
    ("salary_withheld_probation", ("prob. withheld", "probation withheld", "prob withheld", "withheld")),
    ("probation_salary_released", ("prob. rel", "probation released", "prob rel")),
    ("loan_advance_recovery", ("loan", "advance")),
    ("eobi_cost_accrued", ("eobi",)),
    ("income_tax_deduction", ("tax",)),
    ("travelling_allowance", ("travel", "conveyance", "t.a")),
    ("bonus", ("bonus",)),
    ("arrears_other", ("arrear",)),
    ("late_arrival", ("late",)),
    ("leaves_this_month", ("leaves (mo", "leaves this month", "leave days", "leaves")),
    ("days_employed", ("days",)),
    ("monthly_salary", ("monthly salary", "basic salary", "salary")),
    ("emp_id", ("emp id", "emp. id", "employee id", "emp code", "emp#", "emp no", "id")),
    ("employee_name", ("employee name", "name", "employee")),
)
SHEET_LABELS = {"net_pay": "Net Pay", "gross_earned": "Gross", "leave_deduction": "Leave Ded.", "salary_withheld_probation": "Prob. Withheld",
                "probation_salary_released": "Prob. Released", "loan_advance_recovery": "Loan Recovery", "eobi_cost_accrued": "EOBI",
                "income_tax_deduction": "Tax", "travelling_allowance": "Travel", "bonus": "Bonus", "arrears_other": "Arrears",
                "late_arrival": "Late Ded.", "leaves_this_month": "Leaves (Mo.)", "days_employed": "Days", "monthly_salary": "Salary",
                "emp_id": "Emp ID", "employee_name": "Name"}

NET_ADD = ("gross_earned", "travelling_allowance", "bonus", "arrears_other", "probation_salary_released")
NET_LESS = ("leave_deduction", "late_arrival", "loan_advance_recovery", "salary_withheld_probation", "income_tax_deduction")
TEMPLATE_COLUMNS = (  # heading (as the upload recognises it), payroll field or None, width
    ("Sr", None, 5), ("Emp ID", "emp_id", 8), ("Employee Name", "employee_name", 26), ("Staff Type", "staff_type", 13),
    ("Monthly Salary", "monthly_salary", 13), ("Days", "days_employed", 7), ("Gross Salary", "gross_earned", 13),
    ("Travelling", "travelling_allowance", 11), ("Bonus", "bonus", 9), ("Arrears", "arrears_other", 9),
    ("Prob. Released", "probation_salary_released", 12), ("Leave Ded.", "leave_deduction", 11),
    ("Loan Rec.", "loan_advance_recovery", 10), ("Prob. Withheld", "salary_withheld_probation", 12), ("Tax", "income_tax_deduction", 8),
    ("Net Pay", "net_pay", 13), ("EOBI (Firm)", "eobi_cost_accrued", 11), ("Remarks", None, 30),
)

def build_payroll_template(conn, pm_str):
    """Excel template for a month's final payroll sheet, pre-filled with the staff on that month's payroll and the system's
    current figures. Net Pay is a formula; the headings are exactly the ones the upload recognises."""
    import io
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    lines = conn.execute("""SELECT p.*, s.staff_type AS stype FROM payroll_lines p JOIN staff s ON s.emp_id = p.emp_id
                            WHERE p.payroll_month = ? AND p.on_payroll = 1 ORDER BY p.emp_id""", (pm_str,)).fetchall()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = parse_date(pm_str).strftime("%b-%y") + " Payroll"
    bold, white = Font(bold=True), Font(bold=True, color="FFFFFF")
    head_fill, input_fill = PatternFill("solid", fgColor="0F172A"), PatternFill("solid", fgColor="FEF9C3")
    thin = Side(style="thin", color="CBD5E1")
    ws["A1"] = FIRM_NAME
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = f"Final Payroll — {pay_month_label(pm_str)}"
    ws["A2"].font = Font(bold=True, size=12)
    ws["A3"] = "Prepared by: ____________________   Date: __________   (Upload: Monthly Payroll → Upload Final Payroll Sheet)"
    head_row = 5
    col_of = {}
    for c, (heading, field, width) in enumerate(TEMPLATE_COLUMNS, 1):
        cell = ws.cell(head_row, c, heading)
        cell.font, cell.fill = white, head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(c)].width = width
        col_of[heading] = get_column_letter(c)
    first = head_row + 1
    for i, l in enumerate(lines):
        r = first + i
        for c, (heading, field, _) in enumerate(TEMPLATE_COLUMNS, 1):
            if heading == "Sr":
                value = i + 1
            elif field == "staff_type":
                value = l["stype"]
            elif heading == "Net Pay":
                plus = "+".join(f"{col_of[h]}{r}" for h, f, _ in TEMPLATE_COLUMNS if f in NET_ADD)
                minus = "-".join(f"{col_of[h]}{r}" for h, f, _ in TEMPLATE_COLUMNS if f in NET_LESS)
                value = f"={plus}-{minus}"
            elif field == "probation_salary_released":
                value = l[field] + l["security_released"]  # includes held pay / security released to a leaver
            elif field:
                value = l[field]
            else:
                value = None
            cell = ws.cell(r, c, value)
            cell.border = Border(top=thin, bottom=thin, left=thin, right=thin)
            if field in NET_ADD + NET_LESS or field in ("days_employed", "monthly_salary", "eobi_cost_accrued") or heading == "Remarks":
                cell.fill = input_fill  # figures the reviewer finalises
            if field not in (None, "emp_id", "employee_name", "staff_type") or heading == "Net Pay":
                cell.number_format = "#,##0;(#,##0);-"
            if heading == "Net Pay":
                cell.font = bold
    total_row = first + len(lines)
    ws.cell(total_row, 3, "TOTAL").font = bold
    for heading, field, _ in TEMPLATE_COLUMNS:
        if field and field not in ("emp_id", "employee_name", "staff_type", "days_employed"):
            letter = col_of[heading]
            cell = ws[f"{letter}{total_row}"]
            cell.value = f"=SUM({letter}{first}:{letter}{total_row - 1})"
            cell.font, cell.number_format = bold, "#,##0;(#,##0);-"
    ws.freeze_panes = ws.cell(first, 4)
    ws.print_title_rows = f"{head_row}:{head_row}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1

    notes = wb.create_sheet("Instructions")
    notes.column_dimensions["A"].width = 110
    for i, text in enumerate((
        f"How to use this template — {pay_month_label(pm_str)}",
        "",
        "1. The rows are pre-filled with the staff on this month's payroll and the system's current figures. Change any figure to the final amount.",
        "2. Yellow cells are the figures to finalise. Net Pay is calculated: Gross + Travelling + Bonus + Arrears + Prob. Released",
        "   − Leave Ded. − Loan Rec. − Prob. Withheld − Tax. You may overwrite Net Pay with a fixed amount if needed.",
        "3. Do NOT rename the column headings, change Emp IDs, or delete the heading row. Extra columns (e.g. Remarks) are ignored.",
        "4. A person who must not be paid this month: put 0 in Net Pay (or delete the row — anyone not on the sheet is paid nothing).",
        "5. A new person must first be added in Staff Master (with approval) — rows with an unknown Emp ID are not imported.",
        "6. Save the file in Excel (so the formulas are calculated) and give it to the approver.",
        "7. Approver: Monthly Payroll → choose the month → Upload Final Payroll Sheet → check the preview → Apply.",
        "   The month is then approved and locked with these figures; later months follow the system's rules.",
    ), 1):
        notes.cell(i, 1, text).font = Font(bold=(i == 1), size=12 if i == 1 else 11)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("Rs.", "").replace("Rs", "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None

def map_sheet_headings(headings):
    """{column index: payroll field} from a heading row; each field is used once."""
    mapping, used = {}, set()
    for i, h in enumerate(headings):
        text = " ".join(str(h or "").lower().split())
        if not text:
            continue
        for field, words in SHEET_FIELDS:
            if field in used:
                continue
            if any((w == text) if w in ("id", "net", "name") else (w in text) for w in words):
                mapping[i] = field
                used.add(field)
                break
    return mapping

def read_payroll_sheet(conn, filename, content):
    """Find the sheet / heading row of a final payroll sheet and match its rows to staff.
    Returns (sheet name, {heading: field}, {emp_id: {field: value}}, [unmatched row labels])."""
    import io
    if filename.lower().endswith((".xlsx", ".xlsm")) or content.startswith(b"PK\x03\x04"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheets = [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)]) for ws in wb.worksheets]
    else:
        sheets = [("CSV", [list(r) for r in csv.reader(content.decode("utf-8-sig", "replace").splitlines())])]
    staff = conn.execute("SELECT emp_id, employee_name FROM staff").fetchall()
    by_id = {s["emp_id"].upper(): s["emp_id"] for s in staff}
    by_name = {}
    for s in staff:
        by_name.setdefault(name_key(s["employee_name"]), []).append(s["emp_id"])
    best = None
    for title, rows in sheets:
        for h_at, row in enumerate(rows[:20]):
            mapping = map_sheet_headings(row)
            if "net_pay" not in mapping.values() or not ({"emp_id", "employee_name"} & set(mapping.values())):
                continue
            col = {f: i for i, f in mapping.items()}
            matched, unmatched = {}, []
            for r in rows[h_at + 1:]:
                cell = lambda f: r[col[f]] if f in col and col[f] < len(r) else None
                raw_id = str(cell("emp_id") or "").strip().upper()
                raw_name = " ".join(str(cell("employee_name") or "").lower().split())
                if not raw_id and not raw_name:
                    continue
                if raw_name in ("total", "grand total", "totals") or raw_id in ("TOTAL", "GRAND TOTAL"):
                    continue
                emp = by_id.get(raw_id) or (lambda m: m[0] if len(m) == 1 else None)(by_name.get(name_key(raw_name), []))
                values = {f: _num(cell(f)) for f in col if f not in ("emp_id", "employee_name")}
                if values.get("net_pay") is None and any(values.get(f) is not None for f in NET_ADD + NET_LESS):
                    # Net Pay left as a formula never calculated (file not saved in Excel): work it out the same way
                    values["net_pay"] = (sum(values.get(f) or 0 for f in NET_ADD) - sum(values.get(f) or 0 for f in NET_LESS))
                if emp is None:
                    if values.get("net_pay") is not None:
                        unmatched.append(" / ".join(x for x in (raw_id, str(cell("employee_name") or "")) if x))
                    continue
                matched[emp] = values
            if best is None or len(matched) > len(best[2]):
                best = (title, {str(rows[h_at][i]).strip(): f for i, f in mapping.items()}, matched, unmatched)
            break  # first qualifying heading row of this sheet
    if best is None or not best[2]:
        raise ChangeError("sheet_unreadable")
    return best

def apply_payroll_sheet(conn, imp, user, update_salaries=False):
    """Replace the month's payroll with the sheet's figures, then approve and lock the month."""
    pm_str = imp["payroll_month"]
    rows = json.loads(imp["rows"])
    now = datetime.now().isoformat(timespec="seconds")
    if not is_month_locked(conn, pm_str):
        calculate_payroll(pm_str)  # start from up-to-date lines for everyone, then overwrite with the sheet
    banks ={r["bank_name"]: dict(r) for r in conn.execute("SELECT * FROM banks")}
    for line in conn.execute("SELECT * FROM payroll_lines WHERE payroll_month = ?", (pm_str,)).fetchall():
        emp = line["emp_id"]
        if emp in rows:
            values = {f: v for f, v in rows[emp].items() if v is not None and f in SHEET_LABELS}
            if "probation_salary_released" in values:  # the sheet's column includes any security released to a leaver
                values["probation_salary_released"] = max(0.0, values["probation_salary_released"] - line["security_released"])
            note = f"Final figures from {imp['file_name']}."
        elif line["on_payroll"]:
            values = {"net_pay": 0.0}  # paid only if on the final sheet
            note = f"Not on the final sheet {imp['file_name']}: nothing paid."
        else:
            continue  # EOBI-only / not employed: keep as calculated
        net = values.get("net_pay", line["net_pay"])
        s = conn.execute("SELECT bank_name FROM staff WHERE emp_id = ?", (emp,)).fetchone()
        b = banks.get(s["bank_name"] or "", {}) if s else {}
        if line["payment_mode"] == "EOBI only":
            mode = "EOBI only"
        elif net <= 0:
            mode = "Nothing to pay"
        else:
            mode = "Meezan IFT" if (b.get("is_meezan") == 1 or (s and s["bank_name"] == "Meezan Bank")) else "IBFT"
        values.update(payment_mode=mode, validation_checks=note,
                      salary_withheld_timesheet=0.0, timesheet_salary_released=0.0, salary_withheld_absence=0.0, absence_salary_released=0.0,
                      salary_withheld_late=0.0, late_salary_released=0.0)
        conn.execute(f"UPDATE payroll_lines SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ?", (*values.values(), line["id"]))
        if update_salaries and rows.get(emp, {}).get("monthly_salary"):
            conn.execute("UPDATE staff SET monthly_salary = ? WHERE emp_id = ? AND weekly_hours IS NULL", (rows[emp]["monthly_salary"], emp))
    conn.execute("""UPDATE payroll_runs SET status = 'Approved', prepared_by = ?, prepared_date = ?, reviewed_by = ?, reviewed_date = ?,
                    approved_by = ?, approved_date = ?, imported_from = ? WHERE payroll_month = ?""",
                 (f"Final sheet: {imp['file_name']}", imp["uploaded_at"], "Final sheet (prepared outside the system)", imp["uploaded_at"],
                  user, now, imp["file_name"], pm_str))
    conn.execute("UPDATE payroll_imports SET status = 'Discarded' WHERE payroll_month = ? AND status = 'Applied'", (pm_str,))
    conn.execute("UPDATE payroll_imports SET status = 'Applied', applied_at = ? WHERE id = ?", (now, imp["id"]))

# ==============================================================================
# 7c. EMAIL (staff addresses, login details)
# ==============================================================================
# Each Approver/Reviewer/Preparer can connect their own outgoing email account; emails go out from whoever sends them.
# Passwords are stored only as hashes, so the only password that can be emailed is the starting password, and only to
# people who have not yet set their own (it is reset to the starting password at the moment of sending).

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
            f"Your login for the {FIRM_NAME} payroll system is ready.\n\n"
            f"  Address:   {app_url}\n"
            f"  User ID:   {username}\n"
            f"  Password:  {password}\n\n"
            "When you log in for the first time you will be asked to change this password to one of your own.\n"
            "Please do not share your password with anyone.\n\n"
            f"Regards,\n{FIRM_NAME}\n")

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
            f"Regards,\n{account['from_name'] or user}\n{FIRM_NAME}\n")
    results = dict(send_emails(account, [(r["email"], f"{FIRM_SHORT} — gazetted holidays", body.replace("{name}", r["employee_name"]))
                                         for r in recipients]))
    failed = 0
    for r in recipients:
        err = results.get(r["email"])
        failed += err is not None
        conn.execute("INSERT INTO email_log (sent_at, sent_by, from_address, to_address, emp_id, purpose, status, error) VALUES (?,?,?,?,?,?,?,?)",
                     (now, user, account["from_address"], r["email"], r["emp_id"], "Holiday list", "Failed" if err else "Sent", err))
    return "sent" if not failed else ("failed" if failed == len(recipients) else "partly")

AUTH_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — {firm_short} Payroll</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; background: #0f172a; color: #1e293b; min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
  .box {{ background: #fff; border-radius: 10px; padding: 30px 32px; width: 100%; max-width: 360px; margin: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.3); }}
  h1 {{ margin: 0 0 4px 0; font-size: 19px; font-weight: 600; color: #0f172a; }}
  .sub {{ margin: 0 0 20px 0; font-size: 13px; color: #64748b; }}
  form {{ display: flex; flex-direction: column; gap: 14px; }}
  label {{ display: flex; flex-direction: column; font-size: 12px; font-weight: 600; color: #475569; gap: 5px; }}
  input {{ padding: 9px 11px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 14px; }}
  input:focus {{ outline: 2px solid #38bdf8; border-color: #38bdf8; }}
  .btn {{ background: #0284c7; color: #fff; border: none; padding: 10px 14px; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500; margin-top: 4px; }}
  .btn:hover {{ background: #0369a1; }}
  .error {{ background: #fee2e2; color: #991b1b; padding: 9px 12px; border-radius: 6px; font-size: 13px; margin-bottom: 14px; }}
</style>
</head>
<body>
<div class="box">
  <h1>{firm_short} Payroll</h1>
  <p class="sub">{subtitle}</p>
  {error}
  <form method="POST" action="{action}">
    <label>Username <input name="username" value="{username}" required autofocus autocomplete="username"></label>
    <label>Password <input type="password" name="password" required autocomplete="{pw_autocomplete}"></label>
    {confirm}
    <button class="btn" type="submit">{button}</button>
  </form>
</div>
</body>
</html>
"""

def render_auth_page(setup, error="", username=""):
    return AUTH_TEMPLATE.format(
        firm_short=html_lib.escape(FIRM_SHORT),
        title="Create Admin Account" if setup else "Sign In",
        subtitle=(f"First-time setup: create the administrator account (password at least {MIN_PASSWORD_LEN} characters)."
                  if setup else "Sign in to continue."),
        error=f'<div class="error">{html_lib.escape(error)}</div>' if error else "",
        action="/setup" if setup else "/login",
        username=html_lib.escape(username),
        pw_autocomplete="new-password" if setup else "current-password",
        confirm=('<label>Confirm Password <input type="password" name="confirm" required autocomplete="new-password"></label>'
                 if setup else ""),
        button="Create Account" if setup else "Sign In",
    )

# ==============================================================================
# 8. BUILT-IN WEB APPLICATION
# ==============================================================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{firm_short} — Payroll System</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; background: #f4f6f9; color: #1e293b; }}
  header {{ background: #0f172a; color: #fff; padding: 16px 28px; display: flex; justify-content: space-between; align-items: center; }}
  header h1 {{ margin: 0; font-size: 19px; font-weight: 600; }}
  header span {{ font-size: 12.5px; color: #94a3b8; }}
  nav {{ background: #1e293b; padding: 0 28px; display: flex; gap: 6px; }}
  nav a {{ color: #cbd5e1; text-decoration: none; padding: 11px 16px; font-size: 13px; font-weight: 500; border-bottom: 3px solid transparent; }}
  nav a.active, nav a:hover {{ color: #fff; border-bottom-color: #38bdf8; background: rgba(255,255,255,0.05); }}
  .container {{ max-width: 1400px; margin: 22px auto; padding: 0 24px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin-bottom: 22px; }}
  .card {{ background: #fff; border-radius: 8px; padding: 16px 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.07); border-left: 4px solid #0284c7; }}
  .card h3 {{ margin: 0 0 6px 0; font-size: 12px; text-transform: uppercase; color: #64748b; letter-spacing: 0.4px; }}
  .card .val {{ font-size: 22px; font-weight: 700; color: #0f172a; }}
  .card .sub {{ font-size: 12px; color: #64748b; margin-top: 4px; }}
  .panel {{ background: #fff; border-radius: 8px; padding: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.07); margin-bottom: 20px; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid #e2e8f0; text-align: left; white-space: nowrap; }}
  th {{ background: #f8fafc; font-weight: 600; color: #334155; }}
  tr:hover {{ background: #f8fafc; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .badge {{ padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
  .b-warn {{ background: #fef3c7; color: #92400e; }}
  .b-ok {{ background: #dcfce7; color: #166534; }}
  .btn {{ background: #0284c7; color: #fff; border: none; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 500; text-decoration: none; display: inline-block; }}
  .btn:hover {{ background: #0369a1; }}
  .btn-sec {{ background: #475569; }}
  form.inline {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 14px; }}
  label {{ display: flex; flex-direction: column; font-size: 12px; font-weight: 600; color: #475569; gap: 4px; }}
  input, select {{ padding: 7px 10px; border: 1px solid #cbd5e1; border-radius: 5px; font-size: 13px; }}
  .flash {{ padding: 11px 16px; border-radius: 6px; font-size: 13.5px; margin-bottom: 18px; }}
  .flash-ok {{ background: #dcfce7; color: #166534; }}
  .flash-warn {{ background: #fef3c7; color: #92400e; }}
  .runbar {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .btn-ok {{ background: #16a34a; }}
  .btn-ok:hover {{ background: #15803d; }}
  .btn-danger {{ background: #dc2626; }}
  .btn-danger:hover {{ background: #b91c1c; }}
  .note {{ font-size: 12.5px; color: #64748b; margin: 0 0 12px 0; }}
  .modal-bg {{ position: fixed; inset: 0; background: rgba(15,23,42,0.55); display: flex; align-items: center; justify-content: center; z-index: 50; padding: 16px; }}
  .modal {{ background: #fff; border-radius: 10px; padding: 22px 24px; max-width: 640px; width: 100%; max-height: 85vh; overflow: auto; box-shadow: 0 10px 30px rgba(0,0,0,0.3); }}
</style>
</head>
<body>
<header>
  <div>
    <h1>{firm_name}</h1>
    <span>Tax Deduction: Rs. 0 (Disabled) &nbsp;|&nbsp; EOBI: 5% Er + 1% Ee Both Borne by Employer &nbsp;|&nbsp; Travel Rate: Rs. {km_rate}/km</span>
  </div>
  <div style="display:flex; gap:18px; align-items:center;">
    <form method="GET" action="/" style="display:flex; gap:8px; align-items:center;">
      <input type="hidden" name="tab" value="{tab}">
      <label style="color:#cbd5e1; flex-direction:row; align-items:center;">Month:
        <input type="month" name="ym" value="{ym}" onchange="this.form.submit()">
      </label>
      {header_actions}
    </form>
    <form method="POST" action="/logout" style="display:flex; gap:8px; align-items:center;">
      <span>Signed in as <b style="color:#fff;">{user}</b> ({role})</span>
      <button class="btn btn-sec" type="submit">Log out</button>
    </form>
  </div>
</header>
<nav>
  {nav_links}
</nav>
<div class="container">
  {flash}
  {body}
</div>
{popup}
</body>
</html>
"""

class PayrollHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def redirect(self, location, cookie=None):
        self.send_response(302)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def send_html(self, html, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def session_token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else None

    def current_user(self):
        return get_session_user(self.session_token())

    def read_form(self):
        """Form fields as a dict; uploaded files (multipart forms only) go to self.files."""
        self.files = {}
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_FORM_BYTES:
            while length > 0:  # drain an oversized upload so the browser gets the reply
                length -= len(self.rfile.read(min(length, 1 << 20)) or b"x" * length)
            self.files = {"scan": ("too_large", "", b"\0")}  # rejected by the file-type check
            return {}
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            fields, self.files = parse_multipart(content_type, body)
            return fields
        return {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}

    def handle_auth_get(self, path):
        if user_count() == 0:
            if path != "/setup":
                return self.redirect("/setup")
            return self.send_html(render_auth_page(setup=True))
        if path == "/setup":
            return self.redirect("/login")
        if self.current_user():
            return self.redirect("/")
        return self.send_html(render_auth_page(setup=False))

    def handle_auth_post(self, path, data):
        username = normalise_username(data.get("username", "").strip())
        password = data.get("password", "")

        if path == "/logout":
            token = self.session_token()
            if token:
                delete_session(token)
            return self.redirect("/login", cookie=f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")

        if path == "/setup":
            if user_count() > 0:
                return self.redirect("/login")
            error = ""
            if not username:
                error = "Username is required."
            elif len(password) < MIN_PASSWORD_LEN:
                error = f"Password must be at least {MIN_PASSWORD_LEN} characters."
            elif password != data.get("confirm", ""):
                error = "Passwords do not match."
            if error:
                return self.send_html(render_auth_page(setup=True, error=error, username=username), status=400)
            create_user(username, password, role=ROLE_APPROVER)
        elif not verify_login(username, password):
            return self.send_html(render_auth_page(setup=False, error="Invalid username or password.", username=username), status=401)

        token = create_session(username)
        cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS * 3600}"
        return self.redirect("/", cookie=cookie)

    def send_timesheet_view(self, path, qs, pm_str, role, own_emp):
        """The uploaded scan, or the printable daily diary, of one student's month. Staff may only see their own."""
        emp_id = own_emp if role == ROLE_STAFF else qs.get("emp", [""])[0].strip().upper()
        conn = get_conn()
        ts = get_timesheet(conn, emp_id, pm_str) if emp_id else None
        staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone() if emp_id else None
        if ts is None or staff is None:
            conn.close()
            return self.send_html("<p>No timesheet found. <a href='/?tab=timesheets'>Back</a></p>", status=404)

        if path == "/timesheet_file":
            conn.close()
            file_path = os.path.join(UPLOAD_DIR, ts["scan_file"] or "")
            if not ts["scan_file"] or not os.path.isfile(file_path):
                return self.send_html("<p>No scan uploaded. <a href='/?tab=timesheets'>Back</a></p>", status=404)
            with open(file_path, "rb") as f:
                content = f.read()
            safe_name = "".join(c for c in (ts["scan_name"] or "timesheet") if c.isalnum() or c in "._- ") or "timesheet"
            self.send_response(200)
            self.send_header("Content-Type", ts["scan_type"] or "application/octet-stream")
            self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        cfg = get_settings_dict(conn)
        entries = conn.execute("SELECT * FROM timesheet_entries WHERE timesheet_id = ? ORDER BY work_date", (ts["id"],)).fetchall()
        legend_desc = {cat: dict(lst) for cat, lst in timesheet_codes(conn).items()}
        conn.close()
        esc = html_lib.escape
        if ts["mode"] == "Daily":
            rows = ""
            for e in entries:
                d = parse_date(e["work_date"])
                off = e["day_type"] != "Work"
                if off:
                    rows += (f"<tr class='off'><td>{d.strftime('%d-%b-%y')}</td><td></td><td></td><td class='c'><b>{esc(e['day_type']).upper()}</b></td>"
                             "<td></td><td></td><td></td><td></td><td></td></tr>")
                else:
                    rows += (f"<tr><td>{d.strftime('%d-%b-%y')}</td><td>{esc(e['client_name'] or '')}</td><td class='c'>{esc(e['period_ended'] or '')}</td>"
                             f"<td>{esc(e['nature_of_work'] or '').replace(chr(10), '<br>')}</td><td class='c'>{esc(e['line_of_service'] or '')}</td>"
                             f"<td class='c'>{esc(e['head_of_account'] or '')}</td><td class='c'>{esc(e['work_performed'] or '')}</td>"
                             f"<td class='c'>{esc(e['job_code'] or '')}</td><td class='c'>{e['hours']:g}</td></tr>")
            working_days = round(ts["standard_hours"] / daily_hours(cfg)) if daily_hours(cfg) else 0
            used = [(TS_TEXT_FIELDS[cat], code, legend_desc.get(cat, {}).get(code, ""))
                    for cat in TS_CODE_FIELDS for code in sorted({e[cat] for e in entries if e[cat]})]
            legend = ("<p style='margin:14px 0 4px;'><b>Working Legends used:</b></p><table style='width:auto;'><tbody>"
                      + "".join(f"<tr><td>{esc(label)}</td><td class='c'>{esc(code)}</td><td>{esc(desc)}</td></tr>" for label, code, desc in used)
                      + "</tbody></table>") if used else ""
            table = f"""
            <table>
              <thead><tr><th colspan="9" class="black">DAILY DIARY</th></tr>
                <tr class="yellow"><th>Date</th><th>Client's Name</th><th>Year / Period Ended</th><th>Nature of work Undertaken/ Training Received</th>
                <th>Line of Service (1)</th><th>Head of account (2)</th><th>Work Performed (3)</th><th>Job Code (4)</th><th>HOURS</th></tr></thead>
              <tbody>{rows}</tbody>
            </table>
            <p class="tot"><span>Month End Total :</span><b>{ts['total_hours']:g}</b></p>
            <p class="tot"><span>Standard Hours in a month (No. of working days in the month X {daily_hours(cfg):g} hours/day)</span>
              <span>{working_days} &nbsp; {daily_hours(cfg):g}</span><b>{ts['standard_hours']:g}</b></p>{legend}"""
        else:
            table = f"""
            <p>Paper timesheet uploaded as a scan: <a href="/timesheet_file?ym={pm_str[:7]}&emp={esc(emp_id)}">{esc(ts['scan_name'] or 'view scan')}</a></p>
            <table style="width:auto;"><tbody>
              <tr><td>Total hours (as entered)</td><td class="c"><b>{ts['total_hours']:g}</b></td></tr>
              <tr><td>Leave days</td><td class="c">{ts['leave_days']:g}</td></tr>
              <tr><td>Study leave days</td><td class="c">{ts['study_days']:g}</td></tr>
            </tbody></table>"""

        def sign(label, who, when):
            return f"<div class='sig'><div class='line'>{esc(who or '')}<br><small>{(when or '')[:10]}</small></div>{label}</div>"

        page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
        <title>Daily Diary {esc(staff['employee_name'])} {pay_month_label(pm_str)}</title>
        <style>
          body {{ font-family: Calibri, 'Segoe UI', sans-serif; font-size: 12px; margin: 24px; color: #000; background: #fff; }}
          h1 {{ font-size: 13px; margin: 0; }} h2 {{ font-size: 13px; text-align: center; margin: 14px 0; }}
          .meta {{ display: flex; gap: 40px; margin-bottom: 4px; }}
          table {{ border-collapse: collapse; width: 100%; }}
          th, td {{ border: 1px solid #000; padding: 3px 5px; vertical-align: middle; }}
          .black {{ background: #000; color: #fff; }} .yellow th {{ background: #ffff00; }}
          tr.off td {{ background: #92d050; }} .c {{ text-align: center; }}
          .tot {{ display: flex; justify-content: space-between; margin: 6px 0; }}
          .sigs {{ display: flex; justify-content: space-between; margin-top: 50px; gap: 30px; }}
          .sig {{ flex: 1; text-align: center; }} .line {{ border-top: 1px solid #000; padding-top: 4px; min-height: 30px; }}
          .noprint {{ margin-bottom: 14px; }} @media print {{ .noprint {{ display: none; }} body {{ margin: 0; }} }}
        </style></head><body>
        <div class="noprint"><button onclick="window.print()">Print</button> &nbsp; <a href="/?tab=timesheets&ym={pm_str[:7]}&emp={esc(emp_id)}">Back</a>
          &nbsp; Status: <b>{esc(ts['status'])}</b></div>
        <h1>{esc(training_org_of(staff) if staff['staff_type'] in ARTICLE_TYPES else FIRM_NAME)}</h1>
        <h2>DAILY DIARY FOR THE MONTH OF {pay_month_label(pm_str)}</h2>
        {f'''<div class="meta"><span>NAME OF STUDENT: <b>{esc(staff['employee_name'])}</b></span><span>ICAP CRN # {esc(staff['icap_crn'] or '________')}</span>
          <span>DESIGNATION: TRAINEE STUDENT</span></div>''' if staff['staff_type'] in ARTICLE_TYPES else
         f'''<div class="meta"><span>NAME: <b>{esc(staff['employee_name'])}</b></span><span>EMP ID: {esc(staff['emp_id'])}</span>
          <span>DESIGNATION: {esc((staff['designation'] or staff['staff_type'] or '').upper())}</span></div>'''}
        {table}
        {f"<p><b>Remarks:</b> {esc(ts['remarks'])}</p>" if ts['remarks'] else ""}
        <div class="sigs">{sign("Student's Signature", staff['employee_name'], ts['submitted_at'])}{sign("Reviewed by", ts['reviewed_by'], ts['reviewed_at'])}{sign("Approved by", ts['approved_by'], ts['approved_at'])}</div>
        </body></html>"""
        return self.send_html(page)

    def send_staff_document(self, qs, role, own_emp):
        """A stored staff document (CNIC scan, CV, ...). Officers see all; staff only their own."""
        try:
            doc_id = int(qs.get("id", ["0"])[0])
        except ValueError:
            doc_id = 0
        conn = get_conn()
        d = conn.execute("SELECT * FROM staff_documents WHERE id = ?", (doc_id,)).fetchone()
        conn.close()
        if d is None or (role == ROLE_STAFF and d["emp_id"] != own_emp):
            return self.send_html("<p>Document not found. <a href='/'>Back</a></p>", status=404)
        path = os.path.join(UPLOAD_DIR, "documents", d["file"])
        if not os.path.isfile(path):
            return self.send_html("<p>The document file is missing. <a href='/'>Back</a></p>", status=404)
        with open(path, "rb") as f:
            content = f.read()
        safe_name = "".join(c for c in (d["original_name"] or "document") if c.isalnum() or c in "._- ") or "document"
        self.send_response(200)
        self.send_header("Content-Type", d["mime"] or "application/octet-stream")
        self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_travel_receipt(self, qs, role, own_emp):
        """Receipt / screenshot attached to a travel claim. Officers see all; staff only their own."""
        try:
            claim_id = int(qs.get("id", ["0"])[0])
        except ValueError:
            claim_id = 0
        conn = get_conn()
        t = conn.execute("SELECT emp_id, receipt_file, receipt_name, receipt_type FROM travel_claims WHERE id = ?", (claim_id,)).fetchone()
        conn.close()
        path = os.path.join(UPLOAD_DIR, "receipts", t["receipt_file"]) if t and t["receipt_file"] else ""
        if t is None or (role == ROLE_STAFF and t["emp_id"] != own_emp) or not os.path.isfile(path):
            return self.send_html("<p>Receipt not found. <a href='/?tab=travel'>Back</a></p>", status=404)
        with open(path, "rb") as f:
            content = f.read()
        safe_name = "".join(c for c in (t["receipt_name"] or "receipt") if c.isalnum() or c in "._- ") or "receipt"
        self.send_response(200)
        self.send_header("Content-Type", t["receipt_type"] or "application/octet-stream")
        self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def effective_account(self, real_user):
        """(account, user label, staff name being operated or None). While a user operates a delegated portal they act
        with that staff member's account; everything they do is recorded as '<user> for <staff name>'."""
        token = self.session_token()
        conn = get_conn()
        row = conn.execute("SELECT acting_for FROM sessions WHERE token = ?", (token,)).fetchone()
        acting = conn.execute("""SELECT u.username, u.role, u.emp_id, u.must_change_pw, s.employee_name FROM portal_delegates d
                                 JOIN users u ON u.emp_id = d.emp_id JOIN staff s ON s.emp_id = d.emp_id
                                 WHERE d.emp_id = ? AND d.delegate_username = ? AND u.role = ?""",
                              (row["acting_for"], real_user, ROLE_STAFF)).fetchone() if row and row["acting_for"] else None
        conn.close()
        if acting:
            return acting, f"{real_user} for {acting['employee_name']}", acting["employee_name"]
        if row and row["acting_for"]:
            set_session_acting(token, None)  # delegation was removed
        return get_user(real_user), real_user, None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/login", "/setup"):
            return self.handle_auth_get(parsed.path)
        real_user = self.current_user()
        if not real_user:
            return self.redirect("/setup" if user_count() == 0 else "/login")
        account, user, acting = self.effective_account(real_user)
        role, own_emp = account["role"], account["emp_id"]
        is_approver = role == ROLE_APPROVER
        is_staff = role == ROLE_STAFF
        allowed_tabs = ROLE_TABS.get(role, ())

        qs = parse_qs(parsed.query)
        ym = qs.get("ym", ["2026-11"])[0]
        if len(ym) != 7 or not parse_date(f"{ym}-01"):
            ym = "2026-11"
        tab = qs.get("tab", [allowed_tabs[0] if allowed_tabs else "account"])[0]
        pm_str = f"{ym}-01"
        flash_style, flash_text = FLASH_MESSAGES.get(qs.get("msg", [""])[0], ("", ""))
        conn = get_conn()
        km_rate = travel_rate_on(conn, date.today())  # from today's PSO Premier Euro 5 price
        own_row = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (own_emp,)).fetchone() if own_emp else None
        own_timesheets = own_row is not None and timesheet_start(own_row, get_settings_dict(conn)) is not None
        my_portals = delegated_portals(conn, real_user)
        conn.close()
        if acting:
            portal_bar = (f"<div class='flash flash-warn' style='display:flex; justify-content:space-between; align-items:center; gap:10px;'>"
                          f"<span>You are operating <b>{html_lib.escape(acting)}</b>'s portal. Everything you do is recorded as done by "
                          f"<b>{html_lib.escape(real_user)}</b> for them; their requests are decided by the approver.</span>"
                          f"<form method='POST' action='/stop_acting'><button class='btn btn-sec' type='submit'>Return to My Portal</button></form></div>")
        elif my_portals:
            portal_bar = ("<div class='panel' style='display:flex; gap:8px; align-items:center; flex-wrap:wrap; padding:10px 14px;'>"
                          "<span class='note' style='margin:0;'>Portals you operate:</span>" + "".join(
                              f"<form method='POST' action='/act_as'><input type='hidden' name='emp_id' value='{p['emp_id']}'>"
                              f"<button class='btn btn-sec' type='submit'>Operate {html_lib.escape(p['employee_name'])}'s Portal</button></form>"
                              for p in my_portals) + "</div>")
        else:
            portal_bar = ""
        own_cnic_warn = cnic_alert(own_row["cnic_expiry"]) if own_row is not None else ""
        if own_cnic_warn and (is_staff or acting):
            portal_bar += (f"<div class='flash flash-warn'><b>Your {html_lib.escape(own_cnic_warn)}.</b> "
                           "Please give a copy of your renewed CNIC to the office.</div>")
        if is_staff and not own_timesheets:  # only ICAP students, and staff the reviewer has asked for timesheets
            allowed_tabs = tuple(t for t in allowed_tabs if t != "timesheets")
        if account["must_change_pw"] and not acting:  # starting password set by the approver: nothing else until it is changed
            allowed_tabs = ("account",)
            if tab != "account" or parsed.path != "/":
                return self.redirect(f"/?tab=account&ym={ym}&msg=pw_must_change")

        if parsed.path in ("/timesheet_file", "/timesheet_print"):
            return self.send_timesheet_view(parsed.path, qs, pm_str, role, own_emp)
        if parsed.path == "/staff_doc":
            return self.send_staff_document(qs, role, own_emp)
        if parsed.path == "/travel_receipt":
            return self.send_travel_receipt(qs, role, own_emp)
        if parsed.path == "/leave_template":
            if is_staff or acting:
                return self.redirect("/?msg=not_allowed")
            conn = get_conn()
            content = build_leave_template(conn)
            conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", 'attachment; filename="Leave History Template.xlsx"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path == "/payroll_template":
            if is_staff or acting:
                return self.redirect("/?msg=not_allowed")
            conn = get_conn()
            if conn.execute("SELECT COUNT(*) FROM payroll_lines WHERE payroll_month = ?", (pm_str,)).fetchone()[0] == 0:
                conn.close()
                calculate_payroll(pm_str)
                conn = get_conn()
            content = build_payroll_template(conn, pm_str)
            conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="Payroll Sheet {parse_date(pm_str).strftime("%b %Y")}.xlsx"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if parsed.path not in ("/", "/recalc", "/export") or tab not in allowed_tabs:
            return self.redirect(f"/?tab={allowed_tabs[0]}&ym={ym}")
        if parsed.path == "/recalc" and role not in (ROLE_APPROVER, ROLE_PREPARER):
            return self.redirect(f"/?tab={tab}&ym={ym}&msg=not_allowed")
        if parsed.path == "/export" and is_staff:
            return self.redirect(f"/?tab={tab}&ym={ym}&msg=not_allowed")

        if parsed.path == "/recalc":
            calculate_payroll(pm_str)
            self.send_response(302)
            self.send_header("Location", f"/?tab={tab}&ym={ym}")
            self.end_headers()
            return

        if parsed.path == "/export":
            conn = get_conn()
            locked = is_month_locked(conn, pm_str)
            conn.close()
            if not locked:
                return self.redirect(f"/?tab=payroll&ym={ym}&msg=export_locked")
            ibft_p, ift_p = generate_bank_files(pm_str)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = f"<h3>Bank files generated successfully:</h3><ul><li>{ibft_p}</li><li>{ift_p}</li></ul><a href='/?tab=payroll&ym={ym}'>Back to Payroll</a>"
            self.wfile.write(msg.encode("utf-8"))
            return

        conn = get_conn()
        cnt = conn.execute("SELECT COUNT(*) FROM payroll_lines WHERE payroll_month = ?", (pm_str,)).fetchone()[0]
        if cnt == 0:
            conn.close()
            calculate_payroll(pm_str)
            conn = get_conn()
        pending_count = conn.execute("SELECT COUNT(*) FROM change_requests WHERE status = 'Pending'").fetchone()[0]
        approver_only = approver_only_emps(conn)  # the reviewer's own staff record + portals he operates: decided by the approver
        open_credits = conn.execute("""SELECT emp_id, status FROM leave_credits
                                       WHERE status IN ('Applied', 'Late', 'Recommended') AND source != 'Timesheet'""").fetchall()
        pending_credits = sum(1 for c in open_credits if c["status"] == "Recommended" or c["emp_id"] in approver_only)
        credits_for_review = sum(1 for c in open_credits if c["status"] in ("Applied", "Late") and c["emp_id"] not in approver_only)

        def cannot_decide(emp_id):
            """Own items, and for the reviewer those of staff whose portal he operates."""
            return emp_id == own_emp or (role == ROLE_REVIEWER and emp_id in approver_only)

        open_apps = conn.execute("SELECT emp_id, status FROM loan_applications WHERE status IN ('Applied', 'Recommended')").fetchall()
        loans_for_approver = sum(1 for a in open_apps if a["status"] == "Recommended" or a["emp_id"] in approver_only)
        if not is_staff:
            pending_count += pending_credits + loans_for_approver
        loans_waiting = {ROLE_APPROVER: loans_for_approver,
                         ROLE_REVIEWER: sum(1 for a in open_apps if a["status"] == "Applied" and not cannot_decide(a["emp_id"]))}.get(role, 0)

        body = ""
        if tab == "payroll":
            run = conn.execute("SELECT * FROM payroll_runs WHERE payroll_month = ?", (pm_str,)).fetchone()
            stage = run["status"] if run else "Draft"

            def stage_btn(path, label, css, roles):
                if role not in roles:
                    return ""
                return f"<form method='POST' action='{path}?ym={ym}'><button class='btn {css}' type='submit'>{label}</button></form>"

            def signed(who, when):
                return f"<b>{html_lib.escape(who or '')}</b> on {(when or '').replace('T', ' ')}" if who else "—"

            steps = (f"Prepared: {signed(run['prepared_by'], run['prepared_date'])} &nbsp;|&nbsp; "
                     f"Reviewed: {signed(run['reviewed_by'], run['reviewed_date'])} &nbsp;|&nbsp; "
                     f"Approved: {signed(run['approved_by'], run['approved_date'])}") if run else ""
            pending_note = f" <b>{pending_count} change request(s) are still pending.</b>" if pending_count else ""
            if not conn.execute("SELECT 1 FROM attendance_uploads WHERE payroll_month = ?", (pm_str,)).fetchone():
                pending_note += (f" <b>Attendance not uploaded yet</b> — the preparer starts the payroll by uploading the attendance report "
                                 f"(<a href='/?tab=attendance&ym={ym}'>Attendance tab</a>).")
            unreviewed = conn.execute("SELECT COUNT(*), COALESCE(SUM(total_amount), 0) FROM travel_claims WHERE payroll_month = ? AND status = 'Submitted'",
                                      (pm_str,)).fetchone()
            if unreviewed[0]:
                pending_note += (f" <b>{unreviewed[0]} travel claim(s) (Rs. {unreviewed[1]:,.0f}) still await the reviewer</b> — they are not paid "
                                 "this month unless reviewed before the payroll is prepared (otherwise they are paid next month).")
            send_back = stage_btn("/send_back_month", "Send Back to Draft", "btn-danger",
                                  (ROLE_APPROVER,) if stage == "Approved" else (ROLE_APPROVER, ROLE_REVIEWER))
            if stage == "Draft":
                run_text = f"<span class='badge b-warn'>Draft</span> Figures can still change.{pending_note}"
                run_btns = stage_btn("/prepare_month", "Mark as Prepared", "btn-ok", ROLE_ACTIONS["/prepare_month"])
            elif stage == "Prepared":
                run_text = "<span class='badge b-warn'>Prepared — awaiting review</span> Figures are locked."
                run_btns = stage_btn("/review_month", "Mark as Reviewed", "btn-ok", ROLE_ACTIONS["/review_month"]) + send_back
            elif stage == "Reviewed":
                run_text = f"<span class='badge b-warn'>Reviewed — awaiting approval</span> Figures are locked.{pending_note}"
                run_btns = stage_btn("/approve_month", f"Approve {pay_month_label(pm_str)} Payroll", "btn-ok", ROLE_ACTIONS["/approve_month"]) + send_back
            else:
                run_text = "<span class='badge b-ok'>Approved</span> Figures are locked and bank files can be exported."
                run_btns = send_back
            template_btn = f"<a class='btn btn-sec' href='/payroll_template?ym={ym}'>Download Payroll Sheet Template (Excel)</a>"
            runbar = f"""<div class='panel runbar'>
              <div>{pay_month_label(pm_str)} payroll: {run_text}<div class='note' style='margin:6px 0 0 0;'>{steps}</div></div>
              <div style='display:flex; gap:8px; flex-wrap:wrap;'>{template_btn}{run_btns}</div></div>"""
            lines = conn.execute("SELECT * FROM payroll_lines WHERE payroll_month = ? ORDER BY emp_id", (pm_str,)).fetchall()
            tot_gross = sum(r["gross_earned"] for r in lines)
            tot_net = sum(r["net_pay"] for r in lines)
            tot_eobi = sum(r["eobi_cost_accrued"] for r in lines)
            tot_cost = sum(r["total_cost_to_firm"] for r in lines)
            tot_loan = sum(r["loan_advance_recovery"] for r in lines)
            tot_ta = sum(r["travelling_allowance"] for r in lines)

            cards = f"""
            <div class="cards">
              <div class="card"><h3>Active Headcount</h3><div class="val">{sum(r['on_payroll'] for r in lines)}</div><div class="sub">{sum(r['on_probation'] for r in lines)} with pay on hold</div></div>
              <div class="card"><h3>Gross Salary Earned</h3><div class="val">Rs. {tot_gross:,.0f}</div><div class="sub">TA + Bonus: Rs. {tot_ta:,.0f}</div></div>
              <div class="card"><h3>Loan / Adv Recovered</h3><div class="val">Rs. {tot_loan:,.0f}</div><div class="sub">Tax Deducted: Rs. 0</div></div>
              <div class="card"><h3>EOBI Cost (6% Firm)</h3><div class="val">Rs. {tot_eobi:,.0f}</div><div class="sub">5% Er + 1% Ee borne by firm · {sum(1 for r in lines if r['payment_mode'] == 'EOBI only' and r['eobi_cost_accrued'])} EOBI-only (non-employees)</div></div>
              <div class="card" style="border-left-color:#16a34a;"><h3>Net Pay to Transfer</h3><div class="val">Rs. {tot_net:,.0f}</div><div class="sub">Total Cost: Rs. {tot_cost:,.0f}</div></div>
            </div>
            """
            rows_html = ""
            for r in lines:
                chk_badge = f"<span class='badge b-warn'>{r['validation_checks']}</span>" if r["validation_checks"] else "<span class='badge b-ok'>OK</span>"
                if r["leave_balance"] is None:
                    lv_bal = "—"
                else:
                    lv_bal = f"<span class='badge {'b-ok' if r['leave_flag'] == 'OK' else 'b-warn'}'>{r['leave_balance']:g} / {r['leave_limit']:g}</span>"
                rows_html += f"""
                <tr>
                  <td>{r['emp_id']}</td>
                  <td><strong>{r['employee_name']}</strong></td>
                  <td>{r['staff_type'] or '<em>(unset)</em>'}</td>
                  <td class="num">{r['gross_earned']:,.0f}</td>
                  <td class="num">{r['travelling_allowance']:,.0f}</td>
                  <td class="num">{r['probation_salary_released']:,.0f}</td>
                  <td class="num">{f"<span class='badge b-ok'>{r['leave_encashment']:,.0f}</span><br><span class='note'>{r['leave_encash_days']:g} days</span>" if r['leave_encashment'] else 0}</td>
                  <td class="num">{r['leaves_this_month']:g}</td>
                  <td class="num">{r['leaves_cumulative']:g}</td>
                  <td class="num">{lv_bal}</td>
                  <td class="num">{r['leave_deduction']:,.0f}</td>
                  <td class="num">{r['loan_advance_recovery']:,.0f}</td>
                  <td class="num">{r['salary_withheld_probation']:,.0f}</td>
                  <td class="num">{f"<span class='badge b-warn'>{r['salary_withheld_absence']:,.0f}</span>" if r['salary_withheld_absence'] else 0}{f"<br><span class='note'>+{r['absence_salary_released']:,.0f} released</span>" if r['absence_salary_released'] else ''}</td>
                  <td class="num">{f"<span class='badge b-warn'>{r['salary_withheld_late']:,.0f}</span>" if r['salary_withheld_late'] else 0}{f"<br><span class='note'>+{r['late_salary_released']:,.0f} released</span>" if r['late_salary_released'] else ''}</td>
                  <td class="num">{f"<span class='badge b-warn'>{r['salary_withheld_timesheet']:,.0f}</span>" if r['salary_withheld_timesheet'] else 0}{f"<br><span class='note'>+{r['timesheet_salary_released']:,.0f} released</span>" if r['timesheet_salary_released'] else ''}</td>
                  <td class="num"><strong>{r['net_pay']:,.0f}</strong></td>
                  <td class="num">{r['eobi_cost_accrued']:,.0f}</td>
                  <td>{r['payment_mode']}</td>
                  <td>{chk_badge}</td>
                </tr>"""
            holds = conn.execute("""
                SELECT p.emp_id, p.employee_name, p.payroll_month, p.salary_withheld_absence AS held, run.status,
                       COALESCE((SELECT SUM(amount) FROM salary_releases r
                                 WHERE r.emp_id = p.emp_id AND r.withheld_month = p.payroll_month), 0) AS released
                FROM payroll_lines p JOIN payroll_runs run ON run.payroll_month = p.payroll_month
                WHERE p.salary_withheld_absence > 0 AND (run.status = 'Approved' OR p.payroll_month = ?)
                ORDER BY p.payroll_month, p.emp_id
            """, (pm_str,)).fetchall()
            can_release_here = is_approver and stage == "Draft"

            def hold_action(h):
                outstanding = h["held"] - h["released"]
                if outstanding <= 0:
                    return "<span class='badge b-ok'>Released</span>"
                if h["status"] != "Approved":
                    return "Held — can be released once this month is approved"
                if not can_release_here or h["payroll_month"] >= pm_str:
                    return "Held — the approver can release it into a later Draft month"
                return (f"<form method='POST' action='/release_salary?ym={ym}'><input type='hidden' name='emp_id' value='{h['emp_id']}'>"
                        f"<input type='hidden' name='withheld_month' value='{h['payroll_month']}'>"
                        f"<button class='btn btn-ok' type='submit'>Release Rs. {outstanding:,.0f} in {pay_month_label(pm_str)}</button></form>")

            hold_rows = "".join(f"""
                <tr><td>{h['emp_id']}</td><td><strong>{h['employee_name']}</strong></td><td>{pay_month_label(h['payroll_month'])}</td>
                  <td class="num">{h['held']:,.0f}</td><td class="num">{h['released']:,.0f}</td><td>{hold_action(h)}</td></tr>""" for h in holds)
            holds_panel = f"""
            <div class="panel">
              <h3>Withheld Salaries (Unapproved Absence)</h3>
              <p class="note">A recorded unapproved absence withholds the employee's whole net salary for that month. It stays withheld
                until the approver releases it into a later payroll month.</p>
              <table>
                <thead><tr><th>ID</th><th>Employee</th><th>Withheld In</th><th class="num">Withheld</th><th class="num">Released</th><th></th></tr></thead>
                <tbody>{hold_rows}</tbody>
              </table>
            </div>""" if holds else ""

            late_holds = conn.execute("""
                SELECT p.emp_id, p.employee_name, p.payroll_month, p.salary_withheld_late AS held, run.status,
                       r.release_month, r.released_by
                FROM payroll_lines p JOIN payroll_runs run ON run.payroll_month = p.payroll_month
                LEFT JOIN late_hold_releases r ON r.emp_id = p.emp_id AND r.withheld_month = p.payroll_month
                WHERE p.salary_withheld_late > 0 AND (run.status = 'Approved' OR p.payroll_month = ?)
                ORDER BY p.payroll_month, p.emp_id
            """, (pm_str,)).fetchall()
            late_cfg = get_settings_dict(conn)

            def late_action(h):
                if h["release_month"]:
                    return f"<span class='badge b-ok'>Released in {pay_month_label(h['release_month'])}</span>"
                if not is_approver:
                    return "Held — awaiting the approver's release"
                if stage != "Draft":
                    return "Held — the approver can release it into a Draft month"
                if h["payroll_month"] > pm_str or (h["payroll_month"] < pm_str and h["status"] != "Approved"):
                    return "Held"
                return (f"<form method='POST' action='/release_late_hold?ym={ym}' style='display:flex; gap:6px; flex-wrap:wrap; align-items:center;'>"
                        f"<input type='hidden' name='emp_id' value='{h['emp_id']}'><input type='hidden' name='withheld_month' value='{h['payroll_month']}'>"
                        "<input name='note' size='18' placeholder='Reason (optional)'>"
                        f"<button class='btn btn-ok' type='submit'>Release Rs. {h['held']:,.0f} in {pay_month_label(pm_str)}</button></form>")

            def late_dates(h):
                return late_days_text(late_arrival_days(conn, h["emp_id"], h["payroll_month"], late_cfg))

            late_rows = "".join(f"""
                <tr><td>{h['emp_id']}</td><td><strong>{html_lib.escape(h['employee_name'])}</strong></td><td>{pay_month_label(h['payroll_month'])}</td>
                  <td>{late_dates(h)}</td><td class="num">{h['held']:,.0f}</td><td>{late_action(h)}</td></tr>""" for h in late_holds)
            late_panel = f"""
            <div class="panel">
              <h3>Salaries Held for Late Arrivals</h3>
              <p class="note">Late arrivals are never deducted. On {late_cfg.get('late_hold_days', '3')} late arrivals in a calendar month (more than {late_cfg.get('late_grace_minutes', '15')} minutes
                after the person's start time), that month's salary or stipend is held, and only the approver can release it.</p>
              <table>
                <thead><tr><th>ID</th><th>Employee</th><th>Held In</th><th>Late On</th><th class="num">Held (Rs.)</th><th></th></tr></thead>
                <tbody>{late_rows}</tbody>
              </table>
            </div>""" if late_holds else ""

            held_rows = ""
            for s in conn.execute("SELECT * FROM staff WHERE COALESCE(staff_type, '') NOT IN ('Permanent', ?) ORDER BY emp_id", (EOBI_ONLY_TYPE,)).fetchall():
                bal = held_balance(conn, s, pm_str)
                if bal <= 0:
                    continue
                leaving = s["status"] == "Left" or s["leaving_date"]
                on_hold = next((l["on_probation"] for l in lines if l["emp_id"] == s["emp_id"]), 0)
                if s["staff_type"] in ARTICLE_TYPES:
                    why = "Stipend held until the ICAP registration card is received"
                elif on_hold:
                    why = "Pay held until 4 months are complete"
                else:
                    why = "One month's pay held as security until exit"
                action = ""
                left_on = parse_date(s["leaving_date"])
                if is_approver and stage == "Draft" and leaving and not (left_on and fmt_date(first_of_month(left_on)) > pm_str):
                    action = (f"<form method='POST' action='/settle_security?ym={ym}' style='display:flex; gap:6px; flex-wrap:wrap; align-items:center;'>"
                              f"<input type='hidden' name='emp_id' value='{s['emp_id']}'>"
                              "<label style='flex-direction:row; gap:4px; align-items:center;'><input type='checkbox' name='handover' value='1'> Notice served, handover done</label>"
                              "<button class='btn btn-ok' type='submit' name='decision' value='Released'>Release</button>"
                              "<input name='note' size='16' placeholder='Reason to forfeit'>"
                              "<button class='btn btn-danger' type='submit' name='decision' value='Forfeited'>Forfeit</button></form>")
                elif leaving:
                    action = "<span class='note'>The approver settles this in a Draft month</span>"
                held_rows += (f"<tr><td>{s['emp_id']}</td><td><strong>{html_lib.escape(s['employee_name'])}</strong></td><td>{s['staff_type']}</td>"
                              f"<td>{s['status']}{' — leaving ' + s['leaving_date'] if s['leaving_date'] else ''}</td><td>{why}</td>"
                              f"<td class='num'><strong>{bal:,.0f}</strong></td><td>{action}</td></tr>")
            held_panel = f"""
            <div class="panel">
              <h3>Held Pay &amp; Security</h3>
              <p class="note"><b>Internees:</b> pay is released after 4 months, keeping one month as security until they leave after proper notice and
                handing / taking over. <b>ICAP / NOC students:</b> stipend builds up until the ICAP registration card is received; no security.
                <b>Permanent staff:</b> regular pay.</p>
              <table><thead><tr><th>ID</th><th>Name</th><th>Type</th><th>Status</th><th>Why Held</th><th class="num">Held (Rs.)</th><th></th></tr></thead>
                <tbody>{held_rows}</tbody></table>
            </div>""" if held_rows else ""

            import_panel = ""
            if is_approver:
                esc = html_lib.escape
                preview = conn.execute("SELECT * FROM payroll_imports WHERE payroll_month = ? AND status = 'Preview' ORDER BY id DESC LIMIT 1",
                                       (pm_str,)).fetchone()
                applied_from = run["imported_from"] if run else None
                preview_html = ""
                if preview:
                    rows = json.loads(preview["rows"])
                    mapping = json.loads(preview["mapping"])
                    unmatched = json.loads(preview["unmatched"])
                    sys_lines = {l["emp_id"]: l for l in lines}
                    missing = [l for l in lines if l["on_payroll"] and l["emp_id"] not in rows]
                    diff_rows = ""
                    for emp, vals in sorted(rows.items()):
                        sl = sys_lines.get(emp)
                        sheet_net = vals.get("net_pay") or 0
                        sys_net = sl["net_pay"] if sl else 0
                        name = sl["employee_name"] if sl else emp
                        flag = "" if abs(sheet_net - sys_net) < 1 else f"<span class='badge b-warn'>{sheet_net - sys_net:+,.0f}</span>"
                        diff_rows += (f"<tr><td>{emp}</td><td>{esc(name)}</td><td class='num'>{sys_net:,.0f}</td>"
                                      f"<td class='num'><b>{sheet_net:,.0f}</b></td><td class='num'>{flag}</td></tr>")
                    sheet_total = sum((v.get("net_pay") or 0) for v in rows.values())
                    preview_html = f"""
              <div style="border:1px solid #f59e0b; border-radius:8px; padding:12px; margin-top:12px;">
                <h4 style="margin:0 0 6px;">Preview — {esc(preview['file_name'])} (sheet “{esc(preview['sheet_name'])}”), uploaded {preview['uploaded_at'][:16].replace('T', ' ')}</h4>
                <p class="note">Columns recognised: {esc(', '.join(f'{h} → {SHEET_LABELS.get(f, f)}' for h, f in mapping.items()))}.<br>
                  <b>{len(rows)}</b> staff matched; sheet net pay <b>Rs. {sheet_total:,.0f}</b> (system now Rs. {tot_net:,.0f}).
                  {f"<br><span class='badge b-warn'>Not matched to any staff member:</span> {esc(', '.join(unmatched))}" if unmatched else ""}
                  {f"<br><span class='badge b-warn'>On the system payroll but not on the sheet (will be paid nothing):</span> {esc(', '.join(l['emp_id'] + ' ' + l['employee_name'] for l in missing))}" if missing else ""}</p>
                <details><summary style="cursor:pointer;">Net pay, system vs sheet</summary>
                  <table style="margin-top:6px;"><thead><tr><th>ID</th><th>Name</th><th class="num">System</th><th class="num">Sheet</th><th class="num">Difference</th></tr></thead>
                  <tbody>{diff_rows}</tbody></table></details>
                <div style="display:flex; gap:10px; margin-top:10px; flex-wrap:wrap; align-items:center;">
                  <form method="POST" action="/import_payroll_apply?ym={ym}" style="display:flex; gap:10px; align-items:center;">
                    <input type="hidden" name="id" value="{preview['id']}">
                    <label style="flex-direction:row; gap:4px; align-items:center;"><input type="checkbox" name="update_salaries" value="1"> Also update staff monthly salaries from the sheet</label>
                    <button class="btn btn-ok" type="submit">Apply as Final {pay_month_label(pm_str)} Payroll</button>
                  </form>
                  <form method="POST" action="/import_payroll_discard?ym={ym}"><input type="hidden" name="id" value="{preview['id']}">
                    <button class="btn btn-sec" type="submit">Discard</button></form>
                </div>
              </div>"""
                import_panel = f"""
            <div class="panel">
              <h3>Upload Final Payroll Sheet (Excel) — {pay_month_label(pm_str)}</h3>
              <p class="note">For a month finalised outside the system (e.g. November 2026 by the reviewer). The sheet's columns are recognised by
                their headings (Emp ID or Name, and Net Pay; also Gross, Travel, Leave Ded., Loan, Prob. Withheld, EOBI … when present).
                You see a preview first; applying it replaces this month's figures with the sheet's, and approves and locks the month.
                Later months are calculated by the system's own rules.
                {f"<br><b>This month's figures come from the final sheet “{esc(applied_from)}”.</b> Upload a corrected sheet to replace them." if applied_from else ""}</p>
              <form class="inline" method="POST" action="/import_payroll_upload?ym={ym}" enctype="multipart/form-data">
                <label>Final Payroll Sheet <input type="file" name="sheet" accept=".xlsx,.xlsm,.csv" required></label>
                <button class="btn" type="submit">Upload &amp; Preview</button>
              </form>
              {preview_html}
            </div>"""
            body = runbar + import_panel + cards + late_panel + held_panel + holds_panel + f"""
            <div class="panel">
              <table>
                <thead><tr>
                  <th>ID</th><th>Employee</th><th>Type</th><th class="num">Gross</th><th class="num">Travel</th>
                  <th class="num">Prob. Rel.</th><th class="num">Leave Encash.</th><th class="num">Leaves (Mo.)</th><th class="num">Leaves (Cum.)</th><th class="num">Leave Bal. / Limit</th><th class="num">Leave Ded.</th><th class="num">Loan Rec.</th>
                  <th class="num">Prob. Withheld</th><th class="num">Absence Hold</th><th class="num">Late Hold</th><th class="num">Timesheet Hold</th><th class="num">Net Pay</th><th class="num">EOBI (Firm)</th><th>Mode</th><th>Checks</th>
                </tr></thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>"""

        elif tab == "staff":
            staff = conn.execute("SELECT * FROM staff ORDER BY emp_id").fetchall()
            cfg = get_settings_dict(conn)
            eobi_wait = int(float(cfg.get("eobi_permanent_wait_months", 3)))
            std_week_hours = float(cfg.get("standard_weekly_hours", 45))

            month_hours = {s["emp_id"]: weekly_hours_for(conn, s, pm_str) for s in staff}

            def salary_cell(s):
                hours = month_hours[s["emp_id"]]
                if not hours:
                    return f"{s['monthly_salary']:,.0f}"
                paid = round(s["monthly_salary"] * hours / std_week_hours, 0)
                return f"{paid:,.0f}<br><span style='color:#64748b; font-size:11px;'>{s['monthly_salary']:,.0f} × {hours:g}/{std_week_hours:g}h</span>"

            def eobi_status(s):
                if s["status"] == "Left":
                    left = parse_date(s["leaving_date"])
                    return f"Not applicable (left {left.strftime('%d %b %Y')})" if left else "Not applicable (left)"
                stype = s["staff_type"] or ""
                eobi_reg = parse_date(s["eobi_registration_date"])
                start = eobi_start_date(stype, parse_date(s["date_of_joining"]), parse_date(s["icap_reg_confirmed_date"]), eobi_reg, eobi_wait)
                if start and eobi_reg:
                    return f"<span class='badge b-ok'>Registered from {start.strftime('%d %b %Y')}</span>"
                if start:
                    return f"<span class='badge b-warn'>Due from {start.strftime('%d %b %Y')} (register)</span>"
                if stype == "Permanent":
                    return "<span class='badge b-warn'>Enter joining date</span>"
                if stype in ARTICLE_TYPES:
                    return "<span class='badge b-warn'>Awaiting ICAP registration</span>"
                if stype == "Intern":
                    return "Not applicable"
                if stype == EOBI_ONLY_TYPE:
                    return "<span class='badge b-warn'>Enter EOBI registration date</span>"
                return "<span class='badge b-warn'>Set staff type</span>"

            esc = html_lib.escape
            active_staff = [s for s in staff if s["status"] != "Left" and s["staff_type"] != EOBI_ONLY_TYPE]
            missing_docs = {s["emp_id"]: missing_documents(conn, s["emp_id"], s["staff_type"]) for s in active_staff}

            def cnic_cell(s):
                warn = cnic_alert(s["cnic_expiry"])
                if not s["cnic_expiry"]:
                    return "" if s["staff_type"] == EOBI_ONLY_TYPE or s["status"] == "Left" else "<span class='note'>—</span>"
                return f"<span class='badge b-warn'>{warn}</span>" if warn else s["cnic_expiry"]

            def docs_cell(s):
                if s["emp_id"] not in missing_docs:
                    return ""
                miss = missing_docs[s["emp_id"]]
                label = (f"<span class='badge b-warn'>{len(miss)} missing</span>" if miss else "<span class='badge b-ok'>Complete</span>")
                return f"<a href='/?tab=staff&ym={ym}&emp={s['emp_id']}#docs' title='{esc(', '.join(miss), quote=True)}'>{label}</a>"

            rows_html = "".join(f"""
                <tr>
                  <td>{s['emp_id']}</td><td><strong>{s['employee_name']}</strong></td><td>{s['cnic'] or ''}</td><td>{cnic_cell(s)}</td>
                  <td>{s['staff_type'] or ''}{f"<br><span class='note'>{s['leave_entitlement_override'] or 0:g} residual leaves, to {s['articleship_end_date'] or '?'}</span>" if s['staff_type'] == NOC_TYPE else ''}</td>
                  <td>{'n/a' if s['staff_type'] == EOBI_ONLY_TYPE else s['date_of_joining'] or "<span class='badge b-warn'>Not entered</span>"}</td>
                  <td>{s['eobi_registration_date'] or ''}</td><td>{s['eobi_no'] or ''}</td><td>{eobi_status(s)}</td>
                  <td>{s['icap_reg_confirmed_date'] or ''}</td><td>{s['icap_crn'] or ''}</td>
                  <td>{esc(training_org_of(s)) if s['staff_type'] in ARTICLE_TYPES else ''}</td>
                  <td class="num">{month_hours[s['emp_id']] or std_week_hours:g}</td><td class="num">{salary_cell(s)}</td><td>{s['bank_name'] or ''}</td>
                  <td>{s['bank_account_iban'] or ''}</td><td class="num">{s['opening_leaves_cumulative']}</td>
                  <td class="num">{s['opening_withheld_probation']:,.0f}</td><td>{s['status']}</td><td>{docs_cell(s)}</td>
                </tr>""" for s in staff)

            # CNIC alerts, documents of one staff member, and (approver) which documents are required
            alerts = [(s, cnic_alert(s["cnic_expiry"])) for s in active_staff if cnic_alert(s["cnic_expiry"])]
            no_cnic_date = [s for s in active_staff if not s["cnic_expiry"]]
            alerts_panel = ""
            if alerts or no_cnic_date:
                alerts_panel = f"""
            <div class="panel" style="border-left:4px solid #dc2626;">
              <h3>CNIC Alerts</h3>
              {"".join(f"<p class='note' style='margin:2px 0;'><span class='badge b-warn'>{esc(w)}</span> {s['emp_id']} <b>{esc(s['employee_name'])}</b> ({s['cnic'] or 'no CNIC no.'}) — obtain the renewed CNIC and upload it below.</p>" for s, w in alerts)}
              {f"<p class='note' style='margin:6px 0 0 0;'>CNIC expiry date not entered: {esc(', '.join(s['emp_id'] + ' ' + s['employee_name'] for s in no_cnic_date))}.</p>" if no_cnic_date else ""}
            </div>"""
            doc_types_all = conn.execute("SELECT * FROM doc_types ORDER BY active DESC, mandatory DESC, name").fetchall()
            type_names = {t["code"]: t["name"] for t in doc_types_all}
            sel = "".join(c for c in qs.get("emp", [""])[0].upper() if c.isalnum())
            sel_staff = next((s for s in staff if s["emp_id"] == sel), None)
            docs_panel_inner = "<p class='note'>Choose a staff member to see and add their documents.</p>"
            if sel_staff is not None:
                docs = conn.execute("SELECT * FROM staff_documents WHERE emp_id = ? ORDER BY replaced, doc_type, id DESC", (sel,)).fetchall()
                doc_rows = "".join(
                    f"<tr{' style=\"color:#94a3b8;\"' if d['replaced'] else ''}><td>{esc(type_names.get(d['doc_type'], d['doc_type']))}</td>"
                    f"<td><a href='/staff_doc?id={d['id']}' target='_blank'>{esc(d['original_name'] or 'view')}</a></td>"
                    f"<td>{d['expiry_date'] or ''}{(' <span class=\"badge b-warn\">' + esc(cnic_alert(d['expiry_date'])) + '</span>') if d['doc_type'] == 'cnic' and not d['replaced'] and cnic_alert(d['expiry_date']) else ''}</td>"
                    f"<td>{esc(d['uploaded_by'] or '')} {(d['uploaded_at'] or '')[:10]}</td><td>{'Older copy' if d['replaced'] else 'Current'}</td></tr>" for d in docs)
                miss = missing_documents(conn, sel, sel_staff["staff_type"])
                upload_form = ""
                if role in ROLE_ACTIONS["/upload_document"]:
                    opts = "".join(f"<option value='{t['code']}'>{esc(t['name'])}</option>" for t in doc_types_for(conn, sel_staff["staff_type"]))
                    upload_form = f"""
                  <form class="inline" method="POST" action="/upload_document?ym={ym}&emp={sel}" enctype="multipart/form-data">
                    <label>Document <select name="doc_type">{opts}</select></label>
                    <label>File (PDF / JPG / PNG) <input type="file" name="document" accept=".pdf,.jpg,.jpeg,.png" required></label>
                    <label>Expiry Date (CNIC) <input type="date" name="expiry_date"></label>
                    <button class="btn" type="submit">Upload</button>
                  </form>"""
                docs_panel_inner = f"""
                  <p class="note"><b>{sel} {esc(sel_staff['employee_name'])}</b> — CNIC {sel_staff['cnic'] or 'not entered'},
                    expiry {sel_staff['cnic_expiry'] or 'not entered'}.
                    {f"<span class='badge b-warn'>Missing: {esc(', '.join(miss))}</span>" if miss else "<span class='badge b-ok'>All mandatory documents on file</span>"}</p>
                  {upload_form}
                  <table><thead><tr><th>Document</th><th>File</th><th>Expiry</th><th>Uploaded</th><th></th></tr></thead>
                    <tbody>{doc_rows or "<tr><td colspan='5'>No documents yet.</td></tr>"}</tbody></table>"""
            staff_opts = "".join(f"<option value='{s['emp_id']}'{' selected' if s['emp_id'] == sel else ''}>{s['emp_id']} — {esc(s['employee_name'])}</option>"
                                 for s in staff if s["staff_type"] != EOBI_ONLY_TYPE)
            docs_panel = f"""
            <div class="panel" id="docs">
              <h3>Staff Documents</h3>
              <form class="inline" method="GET" action="/">
                <input type="hidden" name="tab" value="staff"><input type="hidden" name="ym" value="{ym}">
                <label>Staff Member <select name="emp" onchange="this.form.submit()"><option value="">(choose)</option>{staff_opts}</select></label>
                <noscript><button class="btn btn-sec" type="submit">Show</button></noscript>
              </form>
              {docs_panel_inner}
            </div>"""
            type_rows = "".join(
                f"<tr{'' if t['active'] else ' style=\"color:#94a3b8;\"'}><td><b>{esc(t['name'])}</b></td><td>{t['applies_to']}</td>"
                f"<td>{'Mandatory' if t['mandatory'] else 'Optional'}</td><td>{'Yes' if t['has_expiry'] else ''}</td><td>{'Active' if t['active'] else 'Not used'}</td>"
                + (f"<td style='display:flex; gap:6px;'>" + "".join(
                    f"<form method='POST' action='/toggle_doc_type?ym={ym}'><input type='hidden' name='code' value='{t['code']}'>"
                    f"<input type='hidden' name='field' value='{f}'><button class='btn btn-sec' type='submit'>{label}</button></form>"
                    for f, label in (("mandatory", "Make Optional" if t["mandatory"] else "Make Mandatory"), ("active", "Stop Using" if t["active"] else "Use Again")))
                   + "</td>" if role in ROLE_ACTIONS["/toggle_doc_type"] else "") + "</tr>" for t in doc_types_all)
            add_type_form = f"""
              <form class="inline" method="POST" action="/save_doc_type?ym={ym}">
                <label>Document <input name="name" required size="26" placeholder="e.g. Character certificate"></label>
                <label>Required For <select name="applies_to"><option>All</option><option>Permanent</option><option value="ICAP Article">ICAP Article (and NOC)</option><option value="NOC Article">NOC Article only</option><option>Intern</option></select></label>
                <label style="flex-direction:row; align-items:center; gap:4px;"><input type="checkbox" name="mandatory" value="1" checked> Mandatory</label>
                <label style="flex-direction:row; align-items:center; gap:4px;"><input type="checkbox" name="has_expiry" value="1"> Has expiry date</label>
                <button class="btn" type="submit">Add Requirement</button>
              </form>""" if role in ROLE_ACTIONS["/save_doc_type"] else ""
            types_panel = f"""
            <div class="panel">
              <h3>Required Documents for New Staff</h3>
              <p class="note">A new staff member can only be submitted with every <b>mandatory</b> document for their staff type attached.
                {'You decide which documents are required.' if is_approver else 'Only the approver can change these requirements.'}</p>
              {add_type_form}
              <table><thead><tr><th>Document</th><th>Required For</th><th></th><th>Expiry</th><th>Status</th>{'<th></th>' if role in ROLE_ACTIONS['/toggle_doc_type'] else ''}</tr></thead>
                <tbody>{type_rows}</tbody></table>
            </div>"""
            new_doc_inputs = "".join(
                f"<label>{esc(t['name'])}{' *' if t['mandatory'] else ''}{'' if t['applies_to'] == 'All' else ' (' + t['applies_to'] + ')'}"
                f" <input type='file' name='doc_{t['code']}' accept='.pdf,.jpg,.jpeg,.png'></label>"
                + (f"<label>{esc(t['name'])} expiry <input type='date' name='expiry_{t['code']}'></label>" if t["has_expiry"] and t["code"] != "cnic" else "")
                for t in doc_types_all if t["active"])
            type_options = "".join(f'<option value="{v}">{label}</option>' for v, label in STAFF_TYPE_OPTIONS)
            training_options = "".join(f'<option value="{esc(o, quote=True)}">{esc(o)}</option>' for o in TRAINING_ORGS)
            approval_note = {ROLE_APPROVER: "Your changes apply immediately and are recorded in the approval log.",
                             ROLE_PREPARER: "<b>Changes you save are sent to the approver and take effect only after approval.</b>",
                             }.get(role, "<b>View only</b> — your role can't change staff or pay details.")
            body = alerts_panel + f"""
            <div class="panel">
              <h3>Update Staff Details (Type, Joining Date, Salary, ICAP Reg Date)</h3>
              <p class="note">{approval_note}</p>
              <p style="font-size:12.5px; color:#64748b; margin:0 0 12px 0;">
                EOBI rules: <b>Permanent</b> — starts {eobi_wait} months after joining &nbsp;|&nbsp;
                <b>ICAP Articleship</b> — starts from the date ICAP confirms registration &nbsp;|&nbsp;
                <b>Internee</b> — no EOBI. Once an <b>EOBI Registration Date</b> is entered, EOBI is charged from that date.
                Leave any field blank to keep its current value.
              </p>
              <form class="inline" method="POST" action="/update_staff?ym={ym}">
                <label>Emp ID <input name="emp_id" placeholder="E001" required size="6"></label>
                <label>Staff Type <select name="staff_type"><option value="">(keep current)</option>{type_options}</select></label>
                <label>Date of Joining (Firm) <input type="date" name="date_of_joining"></label>
                <label>Monthly Salary (full-time rate) <input type="number" name="monthly_salary" placeholder="Leave blank to keep"></label>
                <label>Weekly Hours <input type="number" step="0.5" name="weekly_hours" placeholder="{std_week_hours:g} = full time" style="width:120px;"></label>
                <label>ICAP Card Received <input type="date" name="icap_reg_confirmed_date"></label>
                <label>ICAP CRN <input name="icap_crn" size="8"></label>
                <label>ICAP Training Organisation <select name="training_org"><option value="">(keep current)</option>{training_options}</select></label>
                <label>Residual Leaves (NOC) <input type="number" step="0.5" min="0" name="leave_entitlement_override" style="width:90px;"></label>
                <label>Residual Period Ends (NOC) <input type="date" name="articleship_end_date"></label>
                <label>EOBI Registration Date <input type="date" name="eobi_registration_date"></label>
                <label>Status <select name="status"><option value="">(keep current)</option><option value="Active">Active</option><option value="Left">Left</option></select></label>
                <label>Leaving Date <input type="date" name="leaving_date"></label>
                <label>EOBI No. <input name="eobi_no" size="12"></label>
                <label>Date of Birth <input type="date" name="date_of_birth"></label>
                <label>CNIC Issue Date <input type="date" name="cnic_issue_date"></label>
                <label>CNIC Expiry <input type="date" name="cnic_expiry"></label>
                <button class="btn" type="submit">{'Save Staff' if is_approver else 'Submit for Approval'}</button>
              </form>
            </div>
            <div class="panel">
              <h3>Add New Staff Member</h3>
              <p class="note">A new person is never added to the payroll without the approver's approval. {approval_note}
                <b>Documents marked * are mandatory</b> (PDF, JPG or PNG, up to 10 MB each); the ICAP student card only for ICAP students.
                EOBI-only people (not employees) need no documents.</p>
              <form class="inline" method="POST" action="/add_staff?ym={ym}" enctype="multipart/form-data">
                <label>Emp ID <input name="emp_id" placeholder="E018" required size="6"></label>
                <label>Name <input name="employee_name" required></label>
                <label>CNIC <input name="cnic" placeholder="35202-0000000-0"></label>
                <label>CNIC Issue Date <input type="date" name="cnic_issue_date"></label>
                <label>CNIC Expiry * <input type="date" name="cnic_expiry"></label>
                <label>Designation <input name="designation"></label>
                <label>Staff Type <select name="staff_type" required>{type_options}</select></label>
                <label>Date of Joining (Firm) <input type="date" name="date_of_joining"></label>
                <label>Monthly Salary (full-time rate) <input type="number" name="monthly_salary" placeholder="Blank for EOBI Only"></label>
                <label>EOBI No. <input name="eobi_no" size="12"></label>
                <label>Date of Birth <input type="date" name="date_of_birth"></label>
                <label>EOBI Registration Date <input type="date" name="eobi_registration_date"></label>
                <label>ICAP CRN <input name="icap_crn" size="8"></label>
                <label>ICAP Card Received <input type="date" name="icap_reg_confirmed_date"></label>
                <label>ICAP Training Organisation (ICAP / NOC only) <select name="training_org">{training_options}</select></label>
                <label>Residual Leaves (NOC only) <input type="number" step="0.5" min="0" name="leave_entitlement_override" style="width:90px;"></label>
                <label>Residual Period Ends (NOC only) <input type="date" name="articleship_end_date"></label>
                <label>Weekly Hours <input type="number" step="0.5" name="weekly_hours" placeholder="{std_week_hours:g} = full time" style="width:120px;"></label>
                <label>Bank <input name="bank_name"></label>
                <label>IBAN <input name="bank_account_iban"></label>
                <label>Account Title <input name="account_title"></label>
                {new_doc_inputs}
                <button class="btn" type="submit">{'Add Staff Member' if is_approver else 'Submit for Approval'}</button>
              </form>
            </div>
            {docs_panel}
            {types_panel}
            <div class="panel">
              <table>
                <thead><tr><th>ID</th><th>Name</th><th>CNIC</th><th>CNIC Expiry</th><th>Type</th><th>Date of Joining (Firm)</th><th>EOBI Registration Date</th><th>EOBI No.</th><th>EOBI</th><th>ICAP Card Received</th><th>ICAP CRN</th><th>ICAP Training Organisation</th><th class="num">Hours/Week</th><th class="num">Salary</th><th>Bank</th><th>IBAN</th><th class="num">Cum. Leaves</th><th class="num">Withheld</th><th>Status</th><th>Documents</th></tr></thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>"""

        elif tab == "leaves":
            only_emp = (own_emp or "") if is_staff else None  # Staff see only their own records
            lvs = conn.execute("""SELECT l.*, s.employee_name FROM leave_register l JOIN staff s ON l.emp_id=s.emp_id
                                  WHERE ? IS NULL OR l.emp_id = ? ORDER BY l.id DESC""", (only_emp, only_emp)).fetchall()

            reviewer_emps = approver_only

            def leave_buttons(l):
                """Only the steps decide_leave() would accept for this user."""
                if (role not in ROLE_ACTIONS["/decide_leave"] or is_month_locked(conn, l["payroll_month"])
                        or (cannot_decide(l["emp_id"]) and not (l["status"] == "Recorded" and l["emp_id"] != own_emp))):
                    return []
                st = l["status"]
                steps = []
                if role == ROLE_REVIEWER and st == "Pending":
                    steps.append(("Recommended", "Recommend", "btn-ok"))
                if role == ROLE_APPROVER and (st == "Recommended" or (st == "Pending" and l["emp_id"] in reviewer_emps)):
                    steps.append(("Approved", "Approve", "btn-ok"))
                if st in ("Pending", "Recommended"):
                    steps.append(("Rejected", "Reject", "btn-danger"))
                if st == "Recorded":
                    steps.append(("Cancelled", "Cancel Record", "btn-danger"))
                return steps

            def leave_action(l):
                return "<div style='display:flex; gap:6px;'>" + "".join(
                    f"<form method='POST' action='/decide_leave?ym={ym}'><input type='hidden' name='id' value='{l['id']}'>"
                    f"<input type='hidden' name='decision' value='{d}'><button class='btn {css}' type='submit'>{label}</button></form>"
                    for d, label, css in leave_buttons(l)) + "</div>"

            def leave_status_cell(l):
                st = l["status"]
                who = l["approved_by"] if st in ("Approved", "Rejected", "Cancelled") else l["recommended_by"] if st == "Recommended" else None
                css = "b-ok" if st == "Approved" else "b-warn"
                rec = f"<br><span class='note'>Recommended by {html_lib.escape(l['recommended_by'])}</span>" if l["recommended_by"] and st != "Recommended" else ""
                return f"<span class='badge {css}'>{st}</span>{f' by {html_lib.escape(who)}' if who else ''}{rec}"

            rows_html = "".join(f"""
                <tr>
                  <td>{l['application_date']}</td><td>{l['emp_id']}</td><td>{l['employee_name']}</td>
                  <td>{l['leave_type']}</td><td>{l['date_from']}</td><td>{l['date_to']}</td>
                  <td class="num">{l['no_of_days']}</td><td>{leave_status_cell(l)}</td><td>{l['payroll_month']}</td><td>{html_lib.escape(l['reason'] or '')}{f"<br><span class='note'>filed by {html_lib.escape(l['entered_by'])}</span>" if l['entered_by'] and ' for ' in l['entered_by'] else ''}</td>
                  <td>{leave_action(l)}</td>
                </tr>""" for l in lvs)

            bulk = ""
            if role == ROLE_REVIEWER:
                n = sum(1 for l in lvs if ("Recommended", "Recommend", "btn-ok") in leave_buttons(l))
                if n:
                    bulk = f"<form method='POST' action='/bulk_leave?ym={ym}'><input type='hidden' name='decision' value='Recommended'><button class='btn btn-ok' type='submit'>Recommend All Pending ({n})</button></form>"
            elif role == ROLE_APPROVER:
                n = sum(1 for l in lvs if ("Approved", "Approve", "btn-ok") in leave_buttons(l))
                if n:
                    bulk = f"<form method='POST' action='/bulk_leave?ym={ym}'><input type='hidden' name='decision' value='Approved'><button class='btn btn-ok' type='submit'>Approve All Recommended ({n})</button></form>"

            opening_as_at = parse_date(get_settings_dict(conn).get("opening_balances_as_at"))
            summary = conn.execute("""
                SELECT p.*, s.opening_leaves_cumulative FROM payroll_lines p JOIN staff s ON s.emp_id = p.emp_id
                WHERE p.payroll_month = ? AND (? IS NULL OR p.emp_id = ?) AND COALESCE(p.staff_type, '') != ?
                ORDER BY p.emp_id
            """, (pm_str, only_emp, only_emp, EOBI_ONLY_TYPE)).fetchall()

            enters_for_others = role in (ROLE_APPROVER, ROLE_PREPARER)
            chain_note = ("Every leave is recorded as <b>Pending</b>, recommended by the reviewer, then approved by the approver. "
                          "Nobody can decide their own leave.")
            if enters_for_others:
                leave_who = '<label>Emp ID <input name="emp_id" placeholder="E001" required size="6"></label>'
                leave_types = LEAVE_TYPES
                leave_note = (f"{chain_note} An <b>{ABSENCE_LEAVE_TYPE}</b> is recorded straight away and "
                              "<b>withholds that month's whole salary</b> until the approver releases it.")
            else:
                leave_who = f'<input type="hidden" name="emp_id" value="{own_emp or ""}">'
                leave_types = tuple(t for t in LEAVE_TYPES if t != ABSENCE_LEAVE_TYPE)
                leave_note = (f"You can apply for your own leave only. {chain_note}" if own_emp else FLASH_MESSAGES["no_emp_link"][1])
            type_options = "".join(f"<option>{t}</option>" for t in leave_types)

            def summary_row(r):
                opening = float(r["opening_leaves_cumulative"] or 0)
                if r["leave_balance"] is None:
                    limit_cell, bal_cell = "No limit set", "—"
                else:
                    basis = {"ICAP Article": "whole articleship", NOC_TYPE: "residual leaves"}.get(r["staff_type"], "per year")
                    limit_cell = f"{r['leave_limit']:g} ({basis})"
                    bal_cell = f"<span class='badge {'b-ok' if r['leave_flag'] == 'OK' else 'b-warn'}'>{r['leave_balance']:g} {r['leave_flag']}</span>"
                return f"""
                <tr>
                  <td>{r['emp_id']}</td><td><strong>{r['employee_name']}</strong></td><td>{r['staff_type'] or ''}</td>
                  <td class="num">{opening:g}</td><td class="num">{r['leaves_cumulative'] - opening:g}</td>
                  <td class="num">{r['leaves_this_month']:g}</td><td class="num">{r['leaves_this_year']:g}</td>
                  <td class="num"><strong>{r['leaves_cumulative']:g}</strong></td>
                  <td>{limit_cell}</td><td class="num">{r['leave_carried_forward']:g}</td><td class="num">{r['leave_credit_days']:g}</td><td class="num">{bal_cell}</td>
                  <td class="num">{r['comp_leave_used']:g}</td>
                  <td class="num">{r['excess_leave_days']:g}</td><td class="num">{r['leave_deduction']:,.0f}</td>
                </tr>"""

            leave_import_panel = ""
            if is_approver:
                esc = html_lib.escape
                imp = conn.execute("SELECT * FROM leave_imports WHERE status = 'Preview' ORDER BY id DESC LIMIT 1").fetchone()
                preview = ""
                if imp:
                    d = json.loads(imp["data"])
                    names = {s["emp_id"]: s["employee_name"] for s in conn.execute("SELECT emp_id, employee_name FROM staff")}
                    per = {}
                    for l in d["leaves"]:
                        p = per.setdefault(l["emp_id"], {"n": 0, "days": 0.0, "study": 0.0})
                        p["n"] += 1
                        p["days"] += l["days"]
                        p["study"] += l["days"] if l["leave_type"] == STUDY_LEAVE_TYPE else 0
                    emps = sorted(set(per) | set(d["opening"]))
                    existing = {r[0]: r[1] for r in conn.execute(
                        "SELECT emp_id, COUNT(*) FROM leave_register WHERE date_from >= ? AND status != 'Recorded' GROUP BY emp_id", (LEAVE_HISTORY_FROM,))}
                    def preview_row(e):
                        if e in d["opening"]:
                            days, study = d["opening"][e]
                            opening = f"{days:g}" + (f" ({study:g} study)" if study else "")
                        else:
                            opening = "—"
                        p = per.get(e, {"n": 0, "days": 0, "study": 0})
                        return (f"<tr><td>{e}</td><td>{esc(names.get(e, ''))}</td><td class='num'>{opening}</td>"
                                f"<td class='num'>{p['n']}</td><td class='num'>{p['days']:g}</td><td class='num'>{p['study']:g}</td>"
                                f"<td class='num'>{existing.get(e, 0)}</td></tr>")

                    rows = "".join(preview_row(e) for e in emps)
                    problems = "".join(f"<li>{esc(p)}</li>" for p in d["problems"])
                    preview = f"""
              <div style="border:1px solid #f59e0b; border-radius:8px; padding:12px; margin-top:10px;">
                <h4 style="margin:0 0 6px;">Preview — {esc(imp['file_name'])}</h4>
                <p class="note">Opening balances for <b>{len(d['opening'])}</b> staff (become “as at 31 Dec 2025”); <b>{len(d['leaves'])}</b> leave records from 1 Jan 2026,
                  recorded as <b>approved</b> by you.</p>
                {f"<div class='flash flash-warn'>Rows that will be skipped:<ul style='margin:4px 0;'>{problems}</ul></div>" if problems else ""}
                <table><thead><tr><th>ID</th><th>Name</th><th class="num">Before 1 Jan 2026</th><th class="num">Leaves 2026 (rows)</th>
                  <th class="num">Days</th><th class="num">Of which Study</th><th class="num">2026 records already in the system</th></tr></thead><tbody>{rows}</tbody></table>
                <form method="POST" action="/leave_import_apply?ym={ym}" style="display:flex; gap:10px; align-items:center; margin-top:10px; flex-wrap:wrap;">
                  <input type="hidden" name="id" value="{imp['id']}">
                  <label style="flex-direction:row; gap:4px; align-items:center;"><input type="checkbox" name="replace" value="1" checked>
                    Replace the 2026 leave records already in the system for these staff (recorded absences are kept)</label>
                  <button class="btn btn-ok" type="submit">Apply Leave History</button>
                </form>
                <form method="POST" action="/leave_import_discard?ym={ym}" style="margin-top:6px;"><input type="hidden" name="id" value="{imp['id']}">
                  <button class="btn btn-sec" type="submit">Discard</button></form>
              </div>"""
                leave_import_panel = f"""
            <div class="panel">
              <h3>Upload Leave History</h3>
              <p class="note">Opening balances as at 31 December 2025 plus every leave from 1 January 2026 (study leave as “Exam / Study”).
                <a href="/leave_template?ym={ym}">Download the Leave History template</a>, fill it in, and upload it here. You see a preview first.</p>
              <form class="inline" method="POST" action="/leave_import_upload?ym={ym}" enctype="multipart/form-data">
                <label>Leave History (Excel) <input type="file" name="leave_file" accept=".xlsx" required></label>
                <button class="btn" type="submit">Upload &amp; Preview</button>
              </form>
              {preview}
            </div>"""
            body = leave_import_panel + f"""
            <div class="panel">
              <h3>Leave Summary — {pay_month_label(pm_str)}</h3>
              <p style="font-size:12.5px; color:#64748b; margin:0 0 12px 0;">
                Leave rules: <b>Permanent &amp; Internees</b> — 24 leaves per calendar year; on 1 January unused leave is carried
                forward (leave bank up to 40 days) and days above 40 are paid in January's payroll &nbsp;|&nbsp;
                <b>ICAP Articleship</b> — 130 leaves over the 3 years 6 months tenure &nbsp;|&nbsp;
                <b>NOC Articleship</b> — only the residual leaves, over the residual period &nbsp;|&nbsp;
                Everyone gets <b>2 paid leaves per month</b>; days beyond that are deducted from salary.
                <b>Study leave</b> ("{STUDY_LEAVE_TYPE}") and <b>{URGENT_WORK_ABROAD}</b> are never deducted, but still count towards the balance.
              </p>
              <table>
                <thead><tr><th>ID</th><th>Name</th><th>Type</th>
                  <th class="num">Opening ({opening_as_at.strftime('%d %b %Y') if opening_as_at else 'b/f'})</th>
                  <th class="num">Taken Since</th><th class="num">This Month</th><th class="num">This Year</th>
                  <th class="num">Cumulative</th><th>Entitlement</th><th class="num">B/F (incl.)</th><th class="num">Credits (incl.)</th><th class="num">Balance</th>
                  <th class="num">Credit Used (Mo.)</th><th class="num">Days Deducted</th><th class="num">Deduction (Rs.)</th></tr></thead>
                <tbody>{"".join(summary_row(r) for r in summary)}</tbody>
              </table>
            </div>
            <div class="panel">
              <h3>{'Add Leave Application' if enters_for_others else 'Apply for Leave'} (Auto-applies 25th cut-off rule)</h3>
              <p class="note">{leave_note}</p>
              <form class="inline" method="POST" action="/add_leave?ym={ym}">
                <label>App Date <input type="date" name="application_date" value="{date.today().isoformat()}" required></label>
                {leave_who}
                <label>Type <select name="leave_type">{type_options}</select></label>
                <label>From <input type="date" name="date_from" required></label>
                <label>To <input type="date" name="date_to" required></label>
                <label>Days <input type="number" step="0.5" name="no_of_days" value="1" required style="width:70px;"></label>
                <label>Reason <input name="reason" placeholder="Reason"></label>
                <button class="btn" type="submit">{'Add Leave' if enters_for_others else 'Apply'}</button>
              </form>
            </div>
            <div class="panel">
              <div class="runbar" style="margin-bottom:12px;"><h3 style="margin:0;">Leave Applications</h3>{bulk}</div>
              <table>
                <thead><tr><th>App Date</th><th>ID</th><th>Name</th><th>Type</th><th>From</th><th>To</th><th class="num">Days</th><th>Status</th><th>Payroll Month</th><th>Reason</th><th></th></tr></thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>"""

            esc = html_lib.escape

            # Leave credits: extra leave for holiday / weekend work, recommended by the reviewer, approved by the approver
            credits = conn.execute("""SELECT c.*, s.employee_name, s.staff_type FROM leave_credits c JOIN staff s ON s.emp_id = c.emp_id
                                      WHERE ? IS NULL OR c.emp_id = ? ORDER BY c.id DESC LIMIT 200""", (only_emp, only_emp)).fetchall()

            def credit_status(c):
                label = {"Applied": "With reviewer", "Late": "Late: needs condonation"}.get(c["status"], c["status"])
                css = "b-ok" if c["status"] == "Approved" else "b-warn"
                condoned = f"<br><span class='note'>Delay condoned by {esc(c['condoned_by'])}: {esc(c['condone_note'] or '')}</span>" if c["condoned_by"] else ""
                return f"<span class='badge {css}'>{label}</span>{f' by {esc(c['decided_by'])}' if c['decided_by'] else ''}{condoned}"

            def credit_evidence(c):
                if is_staff:
                    return ""
                a = conn.execute("SELECT status, arrival_time FROM attendance WHERE emp_id = ? AND att_date = ?", (c["emp_id"], c["work_date"])).fetchone()
                text = f"{a['status']}{' at ' + a['arrival_time'] if a['arrival_time'] else ''}" if a else "no record"
                return f"<br><span class='note'>Attendance: {esc(text)}</span>"

            def credit_action(c):
                if c["status"] in ("Applied", "Late"):
                    late = c["status"] == "Late"
                    if c["emp_id"] in approver_only:
                        return credit_review_form(c, ym, "Condone &amp; Approve" if late else "Approve") if role == ROLE_APPROVER else "<span class='note'>Awaiting approver</span>"
                    return credit_review_form(c, ym, "Condone &amp; Recommend" if late else "Recommend") if role == ROLE_REVIEWER else "<span class='note'>Awaiting reviewer</span>"
                if c["status"] != "Recommended":
                    return ""
                if role != ROLE_APPROVER:
                    return "<span class='note'>Awaiting approver</span>"
                return "<div style='display:flex; gap:6px;'>" + "".join(
                    f"<form method='POST' action='/decide_credit?ym={ym}'><input type='hidden' name='id' value='{c['id']}'>"
                    f"<input type='hidden' name='decision' value='{d}'><button class='btn {css}' type='submit'>{label}</button></form>"
                    for d, label, css in (("Approved", "Approve", "btn-ok"), ("Rejected", "Reject", "btn-danger"))) + "</div>"

            credit_rows = "".join(f"""
                <tr><td>{c['emp_id']}</td><td>{esc(c['employee_name'])}</td><td>{parse_date(c['work_date']).strftime('%a %d %b %Y')}</td>
                  <td class="num">{c['days']:g}</td><td>{CREDIT_REASONS.get(c['reason'], c['reason'])}{f"<br><span class='note'>{esc(c['note'])}</span>" if c['note'] else ''}{credit_evidence(c)}</td>
                  <td>{c['source']}</td><td>{esc(c['requested_by'] or '')}</td>
                  <td>{credit_status(c)}</td>
                  <td>{credit_action(c)}</td></tr>""" for c in credits)
            credit_days = int(float(get_settings_dict(conn).get("credit_apply_days", 7)))
            me_row = conn.execute("SELECT staff_type, status FROM staff WHERE emp_id = ?", (own_emp,)).fetchone() if own_emp else None
            apply_credit_form = ""
            if me_row is not None and me_row["status"] == "Active" and me_row["staff_type"] != EOBI_ONLY_TYPE:
                apply_credit_form = f"""
              <h4 style="margin:12px 0 6px;">Apply for Credit Leave</h4>
              <form class="inline" method="POST" action="/apply_credit?ym={ym}">
                <label>Date Worked <input type="date" name="work_date" required max="{date.today().isoformat()}"></label>
                <label>Work Done <input name="note" required size="30" placeholder="e.g. audit closing at client"></label>
                <label style="flex-direction:row; align-items:center; gap:4px;"><input type="checkbox" name="full_day" value="1" required> I worked a complete working day</label>
                <button class="btn" type="submit">Submit Application</button>
              </form>"""
            credit_form = ""
            if role in ROLE_ACTIONS["/add_credit"]:
                reason_opts = "".join(f"<option value='{k}'>{v}</option>" for k, v in CREDIT_REASONS.items())
                credit_form = f"""
              <form class="inline" method="POST" action="/add_credit?ym={ym}">
                <label>Emp ID <input name="emp_id" required size="6" placeholder="E001"></label>
                <label>Date Worked <input type="date" name="work_date" required max="{date.today().isoformat()}"></label>
                <label>Reason <select name="reason">{reason_opts}</select></label>
                <label>Note <input name="note" size="30" placeholder="Work done; after {credit_days} days, why the delay is condoned"></label>
                <button class="btn" type="submit">{'Grant Credit' if role == ROLE_APPROVER else 'Recommend Credit'}</button>
              </form>"""
            body += f"""
            <div class="panel">
              <h3>Leave Credits (Holiday / Weekend Worked)</h3>
              <p class="note">One credit leave for each <b>complete working day</b> spent at work on a gazetted holiday, or on a Saturday / Sunday that is the person's day off.
                <b>Staff apply within {credit_days} days</b> of the date worked; a later application moves forward only if the <b>reviewer condones the delay</b>.
                The reviewer recommends it and the credit counts only after the <b>approver</b> approves it. The same rule applies to <b>all staff, including ICAP / NOC students</b>.
                An approved credit adds to the leave entitlement and covers leave that would otherwise be deducted.</p>
              {apply_credit_form}
              {credit_form}
              <table><thead><tr><th>ID</th><th>Name</th><th>Date Worked</th><th class="num">Days</th><th>Reason</th><th>Source</th><th>By</th><th>Status</th><th></th></tr></thead>
                <tbody>{credit_rows or "<tr><td colspan='9'>No leave credits yet.</td></tr>"}</tbody></table>
            </div>"""

        elif tab == "holidays":
            # Gazetted holidays: everyone sees them; the reviewer (or approver) maintains them and staff are emailed each change
            esc = html_lib.escape
            manage = role in ROLE_ACTIONS["/add_holiday"]
            year = parse_date(pm_str).year
            hols = conn.execute("SELECT * FROM holidays WHERE holiday_date BETWEEN ? AND ? ORDER BY holiday_date",
                                (f"{year}-01-01", f"{year + 1}-12-31")).fetchall()
            hol_rows = "".join(
                f"<tr><td>{parse_date(h['holiday_date']).strftime('%a %d %b %Y')}</td><td><strong>{esc(h['name'])}</strong></td>"
                + (f"<td><form method='POST' action='/delete_holiday?ym={ym}'><input type='hidden' name='holiday_date' value='{h['holiday_date']}'>"
                   f"<button class='btn btn-danger' type='submit'>Remove</button></form></td>" if manage else "") + "</tr>" for h in hols)
            mail_notes = ""
            if manage:
                no_email = conn.execute("""SELECT employee_name FROM staff WHERE status = 'Active' AND COALESCE(email, '') = ''
                                           AND COALESCE(staff_type, '') != ? ORDER BY emp_id""", (EOBI_ONLY_TYPE,)).fetchall()
                if conn.execute("SELECT 1 FROM mail_accounts WHERE username = ?", (user,)).fetchone() is None:
                    mail_notes += "<p class='note'><span class='badge b-warn'>Your sending email account is not set up</span> Holidays will be saved but not emailed until you set it up on the Email tab.</p>"
                if no_email:
                    mail_notes += f"<p class='note'><span class='badge b-warn'>No email address, so not emailed:</span> {esc(', '.join(n['employee_name'] for n in no_email))}</p>"
            hol_form = f"""
              <form class="inline" method="POST" action="/add_holiday?ym={ym}">
                <label>Date <input type="date" name="holiday_date" required></label>
                <label>Holiday <input name="name" required size="28" placeholder="e.g. Quaid-e-Azam Day"></label>
                <button class="btn" type="submit">Save and Email Staff</button>
              </form>
              <form method="POST" action="/email_holidays?ym={ym}" style="margin-bottom:12px;">
                <button class="btn btn-sec" type="submit">Email Holiday List Again</button>
                <span class="note">&nbsp; Adding or removing an upcoming holiday emails the updated list to all staff from your own email account.</span>
              </form>{mail_notes}""" if manage else ""
            body = f"""
            <div class="panel">
              <h3>Gazetted Holidays {year}–{year + 1}</h3>
              <p class="note"><b>Saturday and Sunday are always off.</b> Gazetted holidays show automatically as holidays on timesheets and are not counted as absences or late arrivals.
                Staff working at a client follow the client's holidays: an ICAP student whose client didn't allow a gazetted holiday marks
                that day as <b>Work</b> on the timesheet and applies for credit leave on the Leave Register within 7 days.</p>
              {hol_form}
              <table><thead><tr><th>Date</th><th>Holiday</th>{'<th></th>' if manage else ''}</tr></thead>
                <tbody>{hol_rows or "<tr><td colspan='3'>No holidays entered yet.</td></tr>"}</tbody></table>
            </div>"""

        elif tab == "timesheets":
            cfg = get_settings_dict(conn)
            mode = timesheet_mode(cfg, pm_str)
            esc = html_lib.escape
            emp = own_emp if is_staff else "".join(c for c in qs.get("emp", [""])[0].upper() if c.isalnum())
            status_css = {"Approved": "b-ok"}

            def status_badge(st):
                return f"<span class='badge {status_css.get(st, 'b-warn')}'>{st}</span>"

            list_panel = ""
            if not is_staff:
                students = [r for r in conn.execute("""
                    SELECT p.emp_id, p.employee_name, p.salary_withheld_timesheet, p.timesheet_salary_released, t.status, t.total_hours,
                           t.standard_hours, t.leave_days, t.study_days, t.submitted_at, t.reviewed_by, t.approved_by, t.mode,
                           s.staff_type, s.timesheet_from
                    FROM payroll_lines p JOIN staff s ON s.emp_id = p.emp_id
                    LEFT JOIN timesheets t ON t.emp_id = p.emp_id AND t.payroll_month = p.payroll_month
                    WHERE p.payroll_month = ? AND p.on_payroll = 1 ORDER BY p.emp_id
                """, (pm_str,)).fetchall() if timesheet_required(r, pm_str, cfg)]

                def student_row(r):
                    reg_total, reg_study = register_leave_days(conn, r["emp_id"], pm_str)
                    sheet_leave = f"{(r['leave_days'] or 0) + (r['study_days'] or 0):g}" if r["status"] else "—"
                    held = (f"<span class='badge b-warn'>{r['salary_withheld_timesheet']:,.0f}</span>" if r["salary_withheld_timesheet"] else "0")
                    released = f"<br><span class='note'>+{r['timesheet_salary_released']:,.0f} released</span>" if r["timesheet_salary_released"] else ""
                    asked = "" if r["staff_type"] in ARTICLE_TYPES else "<br><span class='note'>asked by reviewer</span>"
                    return f"""<tr><td>{r['emp_id']}</td><td><strong>{esc(r['employee_name'])}</strong>{asked}</td>
                      <td>{status_badge(r['status'] or 'Not started')}</td><td>{r['mode'] or ''}</td>
                      <td class="num">{f"{r['total_hours']:g}" if r['status'] else '—'}</td>
                      <td class="num">{sheet_leave} / {reg_total:g}</td>
                      <td>{(r['submitted_at'] or '')[:10]}</td><td>{esc(r['reviewed_by'] or '')}</td><td>{esc(r['approved_by'] or '')}</td>
                      <td class="num">{held}{released}</td>
                      <td><a class="btn btn-sec" href="/?tab=timesheets&ym={ym}&emp={r['emp_id']}">Open</a></td></tr>"""

                waiting = {ROLE_REVIEWER: "Submitted", ROLE_APPROVER: "Reviewed"}.get(role)
                n_waiting = sum(1 for r in students if r["status"] == waiting) if waiting else 0
                list_panel = f"""
                <div class="panel">
                  <h3>Timesheets — {pay_month_label(pm_str)}</h3>
                  <p class="note">Each ICAP student (and any other staff member the reviewer has asked) submits a monthly daily diary. It is <b>reviewed by the reviewer</b> and then
                    <b>approved by the approver</b>. Until it is approved the student's whole salary for that month is held; approval
                    releases it (in the same month's payroll while it is still Draft, otherwise automatically in the next one).
                    {"This month is a <b>paper sheet</b>: the student uploads the scan and types in the totals." if mode == "Scanned" else "This month is filled in <b>day by day</b> in the system."}
                    {f"<b>{n_waiting} timesheet(s) waiting for you.</b>" if n_waiting else ""}</p>
                  <table>
                    <thead><tr><th>ID</th><th>Name</th><th>Status</th><th>Type</th><th class="num">Hours</th><th class="num">Leave Days (Sheet / Register)</th>
                      <th>Submitted</th><th>Reviewed By</th><th>Approved By</th><th class="num">Salary Held</th><th></th></tr></thead>
                    <tbody>{"".join(student_row(r) for r in students) or "<tr><td colspan='11'>No timesheets are required for this month.</td></tr>"}</tbody>
                  </table>
                </div>"""
                if role in ROLE_ACTIONS["/set_ts_access"]:
                    others = conn.execute("""SELECT emp_id, employee_name, staff_type, timesheet_from FROM staff
                                             WHERE status = 'Active' AND COALESCE(staff_type, '') NOT IN ('ICAP Article', ?, ?) ORDER BY emp_id""",
                                          (NOC_TYPE, EOBI_ONLY_TYPE)).fetchall()
                    enabled = [o for o in others if o["timesheet_from"]]
                    emp_opts = "".join(f"<option value='{o['emp_id']}'>{o['emp_id']} — {esc(o['employee_name'])} ({o['staff_type']})</option>" for o in others)
                    enabled_rows = "".join(
                        f"<tr><td>{o['emp_id']}</td><td>{esc(o['employee_name'])}</td><td>{o['staff_type']}</td><td>from {pay_month_label(o['timesheet_from'])}</td>"
                        f"<td><form method='POST' action='/set_ts_access?ym={ym}'><input type='hidden' name='emp_id' value='{o['emp_id']}'>"
                        f"<input type='hidden' name='enable' value='0'><button class='btn btn-danger' type='submit'>No Longer Required</button></form></td></tr>"
                        for o in enabled)
                    list_panel += f"""
                <div class="panel">
                  <h3>Timesheets for Other Staff</h3>
                  <p class="note">Timesheets are required only from ICAP students. If the reviewer needs one from another staff member, enable it
                    here: from the chosen month that person gets the Timesheets tab and the <b>same rules apply</b> (submitted, reviewed, approved;
                    salary held until approved). Every change is recorded in the Approval Log.</p>
                  <form class="inline" method="POST" action="/set_ts_access?ym={ym}">
                    <label>Staff Member <select name="emp_id" required>{emp_opts}</select></label>
                    <label>Required From <input type="month" name="from_month" value="{ym}" required></label>
                    <input type="hidden" name="enable" value="1">
                    <button class="btn" type="submit">Require Timesheets</button>
                  </form>
                  {f"<table><thead><tr><th>ID</th><th>Name</th><th>Type</th><th>Timesheets</th><th></th></tr></thead><tbody>{enabled_rows}</tbody></table>" if enabled else "<p class='note'>No other staff are currently asked for timesheets.</p>"}
                </div>"""

            detail = ""
            staff_row = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp,)).fetchone() if emp else None
            if is_staff and not own_emp:
                detail = f"<div class='panel'><p class='note'>{FLASH_MESSAGES['no_emp_link'][1]}</p></div>"
            elif staff_row is not None and not timesheet_required(staff_row, pm_str, cfg) and not get_timesheet(conn, emp, pm_str):
                detail = f"<div class='panel'><p class='note'>{FLASH_MESSAGES['ts_not_icap'][1]}</p></div>"
            elif staff_row is not None:
                ts = get_timesheet(conn, emp, pm_str)
                st = ts["status"] if ts else "Not started"
                can_edit = (role in (ROLE_APPROVER, ROLE_PREPARER) or (is_staff and emp == own_emp)) and st in ("Not started", "Draft", "Returned")
                ts_mode = ts["mode"] if ts else mode
                ro = "" if can_edit else " disabled"
                reg_total, reg_study = register_leave_days(conn, emp, pm_str)
                form_action = f"/save_timesheet?ym={ym}&emp={emp}"

                trail = []
                if ts and ts["submitted_at"]:
                    trail.append(f"Submitted {ts['submitted_at'][:16].replace('T', ' ')} by {esc(ts['prepared_by'] or '')}")
                if ts and ts["reviewed_by"]:
                    trail.append(f"Reviewed by <b>{esc(ts['reviewed_by'])}</b> {ts['reviewed_at'][:16].replace('T', ' ')}")
                if ts and ts["approved_by"]:
                    trail.append(f"Approved by <b>{esc(ts['approved_by'])}</b> {ts['approved_at'][:16].replace('T', ' ')}")
                returned = (f"<div class='flash flash-warn'>Returned by {esc(ts['returned_by'] or '')}: {esc(ts['return_note'] or '')}</div>"
                            if st == "Returned" else "")
                links = ""
                if ts:
                    links = f"<a class='btn btn-sec' href='/timesheet_print?ym={ym}&emp={emp}' target='_blank'>Print Daily Diary</a>"
                    if ts["scan_file"]:
                        links += f" <a class='btn btn-sec' href='/timesheet_file?ym={ym}&emp={emp}' target='_blank'>View Uploaded Scan</a>"

                buttons = (f"<button class='btn' type='submit' name='action' value='save'>Save Draft</button>"
                           f"<button class='btn btn-ok' type='submit' name='action' value='submit'>Submit for Review</button>") if can_edit else ""

                if ts_mode == "Scanned":
                    current = (f"Uploaded: <a href='/timesheet_file?ym={ym}&emp={emp}' target='_blank'>{esc(ts['scan_name'] or 'scan')}</a>"
                               if ts and ts["scan_file"] else "No scan uploaded yet.")
                    form = f"""
                    <p class="note">For {pay_month_label(pm_str)} the timesheet is the paper sheet. Scan it clearly, upload it (PDF, JPG or PNG,
                      up to 10 MB) and type in the month-end total hours and leave days from it. The reviewer checks the figures against the scan.
                      {current}</p>
                    <form class="inline" method="POST" action="{form_action}" enctype="multipart/form-data">
                      <label>Scanned Timesheet <input type="file" name="scan" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"{ro}></label>
                      <label>Month End Total Hours <input type="number" step="0.5" min="0" name="total_hours" value="{(ts['total_hours'] if ts else 0):g}" style="width:110px;"{ro}></label>
                      <label>Leave Days <input type="number" step="0.5" min="0" name="leave_days" value="{(ts['leave_days'] if ts else 0):g}" style="width:80px;"{ro}></label>
                      <label>Study Leave Days <input type="number" step="0.5" min="0" name="study_days" value="{(ts['study_days'] if ts else 0):g}" style="width:80px;"{ro}></label>
                      <label>Remarks <input name="remarks" value="{esc(ts['remarks'] or '') if ts else ''}" size="30"{ro}></label>
                      {buttons}
                    </form>"""
                else:
                    entries = {}
                    if ts:
                        entries = {e["work_date"]: e for e in conn.execute("SELECT * FROM timesheet_entries WHERE timesheet_id = ?", (ts["id"],))}
                    cell = "padding:3px 5px; font-size:12px;"
                    ts_codes = timesheet_codes(conn)
                    hols = month_holidays(conn, pm_str)
                    grid = ""
                    for d in month_days(pm_str):
                        e = entries.get(fmt_date(d))
                        k = d.day
                        day_type = e["day_type"] if e else default_day_type(d, hols)
                        hol_name = hols.get(fmt_date(d))
                        opts = "".join(f"<option{' selected' if t == day_type else ''}>{t}</option>" for t in TS_DAY_TYPES)

                        def txt(col, width):
                            return f"<td><input name='{col}_{k}' value='{esc(e[col] or '', quote=True) if e else ''}' style='{cell} width:{width}px;'{ro}></td>"

                        def pick(col):
                            current = (e[col] or "") if e else ""
                            options = "<option value=''></option>" + "".join(
                                f"<option value='{esc(c, quote=True)}' title='{esc(desc, quote=True)}'{' selected' if c == current else ''}>"
                                f"{esc(c)}{' — ' + esc(desc) if desc else ''}</option>" for c, desc in ts_codes[col])
                            return f"<td><select name='{col}_{k}' style='{cell} max-width:120px;'{ro}>{options}</select></td>"
                        worked_holiday = hol_name and day_type == "Work"
                        bg = (" style='background:#fef3c7;'" if worked_holiday else " style='background:#ecfccb;'" if day_type != "Work" else "")
                        hol_label = ""
                        if hol_name:
                            hol_label = (f"<br><span class='note'><b>{esc(hol_name)}</b>"
                                         f"{' — worked: apply for credit leave within 7 days' if worked_holiday else ''}</span>")
                        grid += (f"<tr{bg}><td>{d.strftime('%d-%b')} <span class='note'>{d.strftime('%a')}</span>{hol_label}</td>"
                                 f"<td><select name='type_{k}' style='{cell}'{ro}>{opts}</select></td>"
                                 + txt("client_name", 140) + txt("period_ended", 50) + txt("nature_of_work", 220) + pick("line_of_service")
                                 + pick("head_of_account") + pick("work_performed") + txt("job_code", 70)
                                 + f"<td><input type='number' step='0.5' min='0' max='24' name='hours_{k}' value='{(e['hours'] if e and e['hours'] else '') if e else ''}' style='{cell} width:55px;'{ro}></td></tr>")
                    totals = (f"Month end total: <b>{ts['total_hours']:g} hours</b> &nbsp;|&nbsp; Standard hours: <b>{ts['standard_hours']:g}</b> "
                              f"(working days × {daily_hours(cfg):g} h) &nbsp;|&nbsp; Leave: {ts['leave_days']:g} &nbsp;|&nbsp; Study leave: {ts['study_days']:g}"
                              if ts else "Totals are worked out when you save.")
                    form = f"""
                    <p class="note"><b>Gazetted holidays and weekends are filled in automatically.</b> If your client didn't allow a gazetted
                      holiday, change that day to <b>Work</b> and enter the work. To earn credit leave for a complete working day on a gazetted holiday, Saturday or Sunday, apply on the Leave Register within 7 days.
                      Fill in one row per day, as on the paper daily diary. Choose the day type; only <b>Work</b> days take a client,
                      nature of work and hours. Choose Line of Service, Head of Account and Work Performed from the lists (the working
                      legends); if a code is missing, ask the reviewer to add it. Save as draft any time; submit once the month is complete.</p>
                    <form method="POST" action="{form_action}">
                      <div style="overflow-x:auto;"><table>
                        <thead><tr><th>Date</th><th>Day Type</th><th>Client's Name</th><th>Year / Period</th><th>Nature of Work / Training</th>
                          <th>Line of Service</th><th>Head of A/c</th><th>Work Perf.</th><th>Job Code</th><th>Hours</th></tr></thead>
                        <tbody>{grid}</tbody>
                      </table></div>
                      <p class="note" style="margin-top:10px;">{totals}</p>
                      <label style="max-width:500px;">Remarks <input name="remarks" value="{esc(ts['remarks'] or '') if ts else ''}"{ro}></label>
                      <div style="display:flex; gap:8px; margin-top:12px;">{buttons}</div>
                    </form>"""

                decide = ""
                if role in ROLE_ACTIONS["/decide_timesheet"] and ts and not cannot_decide(emp):
                    steps = []
                    if role == ROLE_REVIEWER and st == "Submitted":
                        steps.append(("Reviewed", "Mark as Reviewed"))
                    if role == ROLE_APPROVER and (st == "Reviewed" or (st == "Submitted" and emp in approver_only)):
                        steps.append(("Approved", "Approve Timesheet & Release Salary"))
                    can_return = st in ("Submitted", "Reviewed") or (st == "Approved" and role == ROLE_APPROVER and not is_month_locked(conn, pm_str))
                    btns = "".join(f"<form method='POST' action='/decide_timesheet?ym={ym}&emp={emp}'><input type='hidden' name='decision' value='{d}'>"
                                   f"<button class='btn btn-ok' type='submit'>{label}</button></form>" for d, label in steps)
                    if can_return:
                        btns += (f"<form method='POST' action='/decide_timesheet?ym={ym}&emp={emp}' style='display:flex; gap:6px; align-items:flex-end;'>"
                                 f"<input type='hidden' name='decision' value='Returned'><label>Note to student <input name='note' size='34' required></label>"
                                 f"<button class='btn btn-danger' type='submit'>Return for Correction</button></form>")
                    if btns:
                        sheet_leave = (ts["leave_days"] or 0) + (ts["study_days"] or 0)
                        check = (f"Leave register shows <b>{reg_total:g}</b> day(s) this month ({reg_study:g} study); the timesheet shows "
                                 f"<b>{sheet_leave:g}</b> ({ts['study_days']:g} study).")
                        if abs(reg_total - sheet_leave) > 0.01:
                            check = f"<span class='badge b-warn'>Mismatch</span> {check}"
                        worked = timesheet_holiday_work(conn, ts)
                        if worked:
                            check += (f"<br><b>Gazetted holiday(s) worked at client:</b> "
                                      + ", ".join(f"{parse_date(w['work_date']).strftime('%d %b')} ({esc(w['name'])})" for w in worked)
                                      + " — credit leave needs the student's own application on the Leave Register.")
                        decide = f"""<div class="panel"><h3>Review / Approval</h3><p class="note">{check}</p>
                          <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:flex-end;">{btns}</div></div>"""

                detail = f"""
                <div class="panel">
                  <div class="runbar" style="margin-bottom:10px;">
                    <h3 style="margin:0;">Daily Diary — {esc(staff_row['employee_name'])} ({emp}) · {pay_month_label(pm_str)} &nbsp; {status_badge(st)}</h3>
                    <div>{links}</div>
                  </div>
                  <p class="note">ICAP CRN: {esc(staff_row['icap_crn'] or 'not entered')} &nbsp;|&nbsp; {('Training organisation: ' + esc(training_org_of(staff_row)) + ' &nbsp;|&nbsp; ') if staff_row['staff_type'] in ARTICLE_TYPES else ''}{' &nbsp;|&nbsp; '.join(trail) or 'Not submitted yet.'}</p>
                  {returned}
                  {form}
                </div>{decide}"""
            elif emp:
                detail = f"<div class='panel'><p class='note'>{FLASH_MESSAGES['emp_missing'][1]}</p></div>"

            history = ""
            if is_staff and own_emp:
                past = conn.execute("SELECT payroll_month, status, total_hours FROM timesheets WHERE emp_id = ? ORDER BY payroll_month DESC", (own_emp,)).fetchall()
                if past:
                    history = f"""<div class="panel"><h3>My Timesheets</h3><table>
                      <thead><tr><th>Month</th><th>Status</th><th class="num">Hours</th><th></th></tr></thead><tbody>
                      {"".join(f"<tr><td>{pay_month_label(p['payroll_month'])}</td><td>{status_badge(p['status'])}</td><td class='num'>{p['total_hours']:g}</td><td><a class='btn btn-sec' href='/?tab=timesheets&ym={p['payroll_month'][:7]}'>Open</a></td></tr>" for p in past)}
                      </tbody></table></div>"""
            legend_lists = timesheet_codes(conn)
            legend_cols = "".join(
                f"<div style='min-width:220px;'><h4 style='margin:0 0 6px 0;'>{TS_TEXT_FIELDS[cat]}</h4><table><tbody>"
                + "".join(f"<tr><td><b>{esc(c)}</b></td><td>{esc(desc) or '<span class=note>(no description)</span>'}</td></tr>" for c, desc in legend_lists[cat])
                + "</tbody></table></div>" for cat in TS_CODE_FIELDS)
            add_code = ""
            if role in ROLE_ACTIONS["/save_ts_code"]:
                cat_opts = "".join(f"<option value='{cat}'>{TS_TEXT_FIELDS[cat]}</option>" for cat in TS_CODE_FIELDS)
                add_code = f"""
                  <form class="inline" method="POST" action="/save_ts_code?ym={ym}" style="margin-top:14px;">
                    <label>List <select name="category">{cat_opts}</select></label>
                    <label>Code <input name="code" required size="8" placeholder="1100"></label>
                    <label>Description <input name="description" size="40" placeholder="e.g. Audit &amp; Assurance"></label>
                    <button class="btn" type="submit">Add / Update Code</button>
                  </form>
                  <p class="note">Entering an existing code updates its description. Every addition is recorded in the Approval Log.</p>"""
            legends_panel = f"""
            <div class="panel"><details{' open' if add_code and not emp else ''}>
              <summary style="cursor:pointer; font-weight:600;">Working Legends — timesheet drop-down codes</summary>
              <div style="display:flex; gap:24px; flex-wrap:wrap; margin-top:12px; align-items:flex-start;">{legend_cols}</div>
              {add_code}
            </details></div>"""
            body = detail + list_panel + history + legends_panel

        elif tab == "attendance":
            esc = html_lib.escape
            can_act = role in ROLE_ACTIONS["/upload_attendance"]
            uploads = conn.execute("SELECT * FROM attendance_uploads WHERE payroll_month = ? ORDER BY id DESC", (pm_str,)).fetchall()
            summary = attendance_summary(conn, pm_str)
            staff_rows = {s["emp_id"]: s for s in conn.execute("SELECT * FROM staff")}
            on_payroll = [l for l in conn.execute("SELECT emp_id, employee_name FROM payroll_lines WHERE payroll_month = ? AND on_payroll = 1 ORDER BY emp_id",
                                                  (pm_str,))]
            not_in_report = [l for l in on_payroll if l["emp_id"] not in summary]
            unmatched = json.loads(uploads[0]["unmatched"] or "[]") if uploads else []
            upload_list = "".join(
                f"<li>{esc(u['file_name'] or '')} — {u['period_from'] or '?'} to {u['period_to'] or '?'}, by {esc(u['uploaded_by'])} on {u['uploaded_at'][:16].replace('T', ' ')}</li>"
                for u in uploads)
            map_form = ""
            if unmatched and can_act:
                emp_opts = "".join(f"<option value='{e}'>{e} — {esc(s['employee_name'])}</option>" for e, s in sorted(staff_rows.items())
                                   if s["staff_type"] != EOBI_ONLY_TYPE)
                map_form = "".join(
                    f"<form class='inline' method='POST' action='/map_attendance?ym={ym}' style='margin:4px 0;'>"
                    f"<input type='hidden' name='code' value='{esc(u.split(' ', 1)[0], quote=True)}'><span><b>{esc(u)}</b> is</span>"
                    f"<select name='emp_id'>{emp_opts}</select><button class='btn btn-sec' type='submit'>Match</button></form>" for u in unmatched)

            def att_row(emp, a):
                s = staff_rows.get(emp)
                left = s is not None and s["status"] == "Left"
                actions = ""
                if can_act and a["uncovered"] and not left:
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
            body = f"""
            <div class="panel">
              <h3>Attendance — {pay_month_label(pm_str)}</h3>
              <p class="note"><b>The payroll starts here:</b> the preparer uploads the attendance software's <i>Monthly Performance Report</i>
                (PDF). Each person's daily status is saved; absences not covered by a leave application or a gazetted holiday are listed below
                to be filed as leave (then recommended by the reviewer and approved by the approver) or recorded as unapproved absence.
                The month can't be marked as prepared until its attendance is uploaded. A later report for the same month updates the days it covers.</p>
              {f'''<form class="inline" method="POST" action="/upload_attendance?ym={ym}" enctype="multipart/form-data">
                <label>Attendance Report (PDF) <input type="file" name="report" accept=".pdf,application/pdf" required></label>
                <button class="btn" type="submit">Upload Attendance</button>
              </form>''' if can_act else ''}
              {f"<p class='note'><b>Reports uploaded for this month:</b></p><ul class='note'>{upload_list}</ul>" if uploads else "<p class='note'><span class='badge b-warn'>No attendance uploaded for this month yet.</span></p>"}
              {f"<div class='flash flash-warn'>Not matched to any staff member (match each once; the system remembers the code):</div>{map_form or esc(', '.join(unmatched))}" if unmatched else ""}
              {f"<p class='note'><span class='badge b-warn'>On the payroll but not in the attendance report:</span> {esc(', '.join(l['emp_id'] + ' ' + l['employee_name'] for l in not_in_report))}</p>" if uploads and not_in_report else ""}
            </div>
            <div class="panel">
              <table><thead><tr><th>ID</th><th>Name</th><th class="num">Days</th><th class="num">Present</th><th class="num">Late</th>
                <th class="num">Absent</th><th class="num">Covered by Leave / Holiday</th><th>Absent — No Leave Filed</th><th></th></tr></thead>
                <tbody>{rows or "<tr><td colspan='9'>No attendance for this month yet.</td></tr>"}</tbody></table>
              <p class="note" style="margin-top:8px;">Late = arriving more than {esc(cfg_att.get('late_grace_minutes', '15'))} minutes after the person's start time. Late arrivals are never deducted.
                On {esc(cfg_att.get('late_hold_days', '3'))} late arrivals on working days in a calendar month (counted from {esc(cfg_att.get('late_hold_from', ''))}) that month's salary or stipend is held
                until the approver releases it on the Monthly Payroll tab.</p>
            </div>""" + hours_panel + timings_panel

        elif tab == "email":
            esc = html_lib.escape
            cfg = get_settings_dict(conn)
            app_url = cfg.get("app_url", f"http://127.0.0.1:{PORT}")
            people = conn.execute("""
                SELECT s.emp_id, s.employee_name, s.email, u.username, u.must_change_pw,
                       (SELECT MAX(sent_at) FROM email_log l WHERE l.emp_id = s.emp_id AND l.purpose = 'Login details' AND l.status = 'Sent') AS last_sent
                FROM staff s LEFT JOIN users u ON u.emp_id = s.emp_id
                WHERE s.status = 'Active' AND COALESCE(s.staff_type, '') != ? ORDER BY s.emp_id
            """, (EOBI_ONLY_TYPE,)).fetchall()
            account = conn.execute("SELECT * FROM mail_accounts WHERE username = ?", (user,)).fetchone()
            sendable = [p for p in people if p["username"] and p["must_change_pw"] and p["email"]]

            def login_cell(p):
                if not p["username"]:
                    return "No login"
                if not p["must_change_pw"]:
                    return "<span class='badge b-ok'>Using own password</span>"
                return "<span class='badge b-warn'>Starting password</span>"

            def send_cell(p):
                if not (p["username"] and p["must_change_pw"] and p["email"]):
                    return ""
                return (f"<form method='POST' action='/send_logins?ym={ym}'><input type='hidden' name='emp_id' value='{p['emp_id']}'>"
                        f"<button class='btn btn-sec' type='submit'{'' if account else ' disabled'}>Send Login</button></form>")

            people_rows = "".join(f"""
                <tr><td>{p['emp_id']}</td><td><strong>{esc(p['employee_name'])}</strong></td><td>{esc(p['username'] or '')}</td>
                  <td>{esc(p['email'] or '') or "<span class='badge b-warn'>No email</span>"}</td><td>{login_cell(p)}</td>
                  <td>{(p['last_sent'] or '')[:16].replace('T', ' ')}</td><td>{send_cell(p)}</td></tr>""" for p in people)

            report_html = ""
            if cfg.get("last_email_upload"):
                rep = json.loads(cfg["last_email_upload"])
                lst = lambda items: "<br>".join(esc(i) for i in items) or "—"
                report_html = f"""<div class="note" style="margin-top:10px;">Last upload: <b>{esc(rep.get('file', ''))}</b> by {esc(rep.get('by', ''))}
                  on {rep.get('at', '')[:16].replace('T', ' ')} — <b>{len(rep['updated'])}</b> updated,
                  <b>{len(rep['unmatched'])}</b> not matched, <b>{len(rep['invalid'])}</b> invalid.
                  {f"<br><b>Not matched to any staff member:</b><br>{lst(rep['unmatched'])}" if rep['unmatched'] else ""}
                  {f"<br><b>Invalid email:</b><br>{lst(rep['invalid'])}" if rep['invalid'] else ""}</div>"""

            sec_opts = lambda cur: "".join(f"<option{' selected' if s == cur else ''}>{s}</option>" for s in MAIL_SECURITY)
            a = account
            account_state = (f"<span class='badge b-ok'>Connected: {esc(a['from_address'])} via {esc(a['smtp_host'])}:{a['smtp_port']}</span>"
                             if a else "<span class='badge b-warn'>Not set up yet</span>")
            log = conn.execute("SELECT * FROM email_log ORDER BY id DESC LIMIT 60").fetchall()
            names = {p["emp_id"]: p["employee_name"] for p in people}
            log_rows = "".join(f"""
                <tr><td>{l['sent_at'][:16].replace('T', ' ')}</td><td>{esc(l['sent_by'])}</td><td>{esc(l['from_address'] or '')}</td>
                  <td>{esc(l['to_address'] or '')}{f" ({esc(names.get(l['emp_id'], l['emp_id']))})" if l['emp_id'] else ''}</td><td>{l['purpose']}</td>
                  <td><span class='badge {'b-ok' if l['status'] == 'Sent' else 'b-warn'}'>{l['status']}</span></td>
                  <td style="white-space:normal; max-width:360px;">{esc(l['error'] or '')}</td></tr>""" for l in log)
            preview = login_email_text("<Name>", "<CNIC without dashes>", "<Starting password>", app_url)

            body = f"""
            <div class="panel">
              <h3>Staff Email Addresses</h3>
              <p class="note">Upload the email list as <b>CSV or Excel (.xlsx)</b>. It needs an <b>Email</b> column and one of
                <b>Emp ID</b>, <b>CNIC</b> or <b>Name</b> to match each row to a staff member. Uploading again updates the addresses.</p>
              <form class="inline" method="POST" action="/upload_emails?ym={ym}" enctype="multipart/form-data">
                <label>Email List <input type="file" name="email_list" accept=".csv,.xlsx" required></label>
                <button class="btn" type="submit">Upload List</button>
              </form>
              <form class="inline" method="POST" action="/set_email?ym={ym}">
                <label>Emp ID <input name="emp_id" required size="6" placeholder="E005"></label>
                <label>Email <input type="email" name="email" size="30" placeholder="name@example.com"></label>
                <button class="btn btn-sec" type="submit">Save One Address</button>
              </form>
              {report_html}
            </div>
            <div class="panel">
              <h3>My Sending Email Account &nbsp; {account_state}</h3>
              <p class="note">Emails go out <b>from the account of whoever sends them</b> (preparer, reviewer or approver). For <b>Gmail</b> use
                server smtp.gmail.com, port 587, STARTTLS and a Google <b>App Password</b> (Google Account → Security → 2-Step Verification →
                App passwords), not your normal password. For <b>Outlook / Microsoft 365</b>: smtp.office365.com, 587, STARTTLS.
                The password is kept only on this computer and is never shown again; leave it blank to keep the saved one.</p>
              <form class="inline" method="POST" action="/save_mail_account?ym={ym}">
                <label>Your Email <input type="email" name="from_address" value="{esc(a['from_address']) if a else ''}" required size="26"></label>
                <label>Display Name <input name="from_name" value="{esc(a['from_name'] or '') if a else esc(FIRM_SHORT)}" size="22"></label>
                <label>SMTP Server <input name="smtp_host" value="{esc(a['smtp_host']) if a else 'smtp.gmail.com'}" required size="18"></label>
                <label>Port <input type="number" name="smtp_port" value="{a['smtp_port'] if a else 587}" required style="width:80px;"></label>
                <label>Security <select name="security">{sec_opts(a['security'] if a else 'STARTTLS')}</select></label>
                <label>Login (if different) <input name="smtp_user" value="{esc(a['smtp_user'] or '') if a and a['smtp_user'] != a['from_address'] else ''}" size="18"></label>
                <label>App Password <input type="password" name="smtp_password" autocomplete="new-password" placeholder="{'(saved)' if a and a['smtp_password'] else ''}" size="16"></label>
                <button class="btn" type="submit">Save Account</button>
              </form>
              {f"<form method='POST' action='/send_test_email?ym={ym}'><button class='btn btn-sec' type='submit'>Send Test Email to Myself</button></form>" if a else ""}
            </div>
            <div class="panel">
              <h3>Send Login Details</h3>
              <p class="note">Sends each person their <b>user ID</b> (CNIC without dashes), <b>starting password</b> and the payroll address.
                Only people who <b>haven't set their own password yet</b> can be sent one: passwords are stored scrambled, so the system can only
                send the starting password (it is reset to the starting password at that moment). Everyone must change it at first login.</p>
              <form class="inline" method="POST" action="/set_app_url?ym={ym}">
                <label>Payroll Address in Emails <input name="app_url" value="{esc(app_url)}" size="34" required></label>
                <button class="btn btn-sec" type="submit">Save Address</button>
              </form>
              {f'''<form class="inline" method="POST" action="/set_approver_email?ym={ym}">
                <label>Approver's Email (notified when a leave credit awaits approval) <input type="email" name="approver_email" value="{esc(cfg.get('approver_notify_email', ''))}" size="30"></label>
                <button class="btn btn-sec" type="submit">Save</button>
              </form>''' if role in ROLE_ACTIONS["/set_approver_email"] else ""}
              <p class="note">Set this to the office address once the system is deployed (now it is only reachable on this computer).</p>
              <form method="POST" action="/send_logins?ym={ym}" style="margin-bottom:14px;">
                <button class="btn btn-ok" type="submit"{'' if account and sendable else ' disabled'}>Send Login Details to All ({len(sendable)})</button>
                {"" if account else "<span class='note'>&nbsp; Set up your sending email account first.</span>"}
              </form>
              <details><summary style="cursor:pointer;">Preview of the email</summary><pre style="background:#f8fafc; padding:12px; border-radius:6px;">{esc(preview)}</pre></details>
              <table style="margin-top:12px;">
                <thead><tr><th>ID</th><th>Name</th><th>User ID</th><th>Email</th><th>Login</th><th>Login Email Sent</th><th></th></tr></thead>
                <tbody>{people_rows}</tbody>
              </table>
            </div>
            <div class="panel">
              <h3>Email Log</h3>
              <table>
                <thead><tr><th>When</th><th>Sent By</th><th>From</th><th>To</th><th>Purpose</th><th>Status</th><th>Reason (if failed)</th></tr></thead>
                <tbody>{log_rows or "<tr><td colspan='7'>No emails sent yet.</td></tr>"}</tbody>
              </table>
            </div>"""

        elif tab == "travel":
            only_emp = (own_emp or "") if is_staff else None  # Staff see only their own records
            trs = conn.execute("""SELECT t.*, s.employee_name FROM travel_claims t JOIN staff s ON t.emp_id=s.emp_id
                                  WHERE ? IS NULL OR t.emp_id = ? ORDER BY t.id DESC""", (only_emp, only_emp)).fetchall()
            esc = html_lib.escape
            chain_note = ("Every claim goes to the <b>reviewer</b>, who marks it reviewed and decides whether it is an out-of-pocket expense "
                          "<b>chargeable to the client</b>. A claim is paid in payroll only once reviewed.")
            if role in (ROLE_APPROVER, ROLE_PREPARER):
                travel_who, travel_note = '<label>Emp ID <input name="emp_id" placeholder="E008" required size="6"></label>', chain_note
            else:
                travel_who = f'<input type="hidden" name="emp_id" value="{own_emp or ""}">'
                travel_note = (f"You can submit your own travel claims only. {chain_note}" if own_emp else FLASH_MESSAGES["no_emp_link"][1])
            can_review = role in ROLE_ACTIONS["/review_travel"]

            def claim_status(t):
                st = t["status"] or "Submitted"
                badge = f"<span class='badge {'b-ok' if st == 'Reviewed' else 'b-warn'}'>{st}</span>"
                if st == "Reviewed":
                    badge += f"<br><span class='note'>by {esc(t['reviewed_by'] or '')}</span>"
                if st == "Rejected":
                    badge += f"<br><span class='note'>{esc(t['review_note'] or '')}</span>"
                return badge

            def claim_charge(t):
                if t["status"] != "Reviewed":
                    return "—"
                if not t["chargeable"]:
                    return "Firm expense"
                inv = (f"<br><span class='badge b-ok'>Invoiced{' ' + esc(t['invoice_no']) if t['invoice_no'] else ''}</span>" if t["invoiced"]
                       else "<br><span class='note'>not yet invoiced</span>")
                return f"<b>Chargeable (OPE)</b>{inv}"

            def claim_action(t):
                if t["status"] != "Submitted":
                    return ""
                out = ""
                if can_review and not cannot_decide(t["emp_id"]):
                    out += (f"<form method='POST' action='/review_travel?ym={ym}' style='display:flex; gap:4px; align-items:center;'>"
                            f"<input type='hidden' name='id' value='{t['id']}'><input type='hidden' name='decision' value='Reviewed'>"
                            f"<input name='client_name' value='{esc(t['client_name'] or '', quote=True)}' placeholder='Client' size='12' title='Client'>"
                            f"<select name='chargeable'><option value='0'>Not chargeable</option><option value='1'>Chargeable to client (OPE)</option></select>"
                            f"<button class='btn btn-ok' type='submit'>Review</button></form>"
                            f"<form method='POST' action='/review_travel?ym={ym}' style='display:flex; gap:4px; margin-top:4px;'>"
                            f"<input type='hidden' name='id' value='{t['id']}'><input type='hidden' name='decision' value='Rejected'>"
                            f"<input name='note' placeholder='Reason for rejecting' size='18'><button class='btn btn-danger' type='submit'>Reject</button></form>")
                if t["emp_id"] == own_emp or role in (ROLE_APPROVER, ROLE_PREPARER):
                    out += (f"<form method='POST' action='/withdraw_travel?ym={ym}' style='margin-top:4px;'><input type='hidden' name='id' value='{t['id']}'>"
                            f"<button class='btn btn-sec' type='submit'>Withdraw</button></form>")
                return out

            def mode_cells(t):
                receipt = f"<br><a href='/travel_receipt?id={t['id']}' target='_blank'>receipt</a>" if t["receipt_file"] else ""
                if (t["travel_mode"] or OWN_VEHICLE) == OWN_VEHICLE:
                    return (f"<td>{OWN_VEHICLE}{receipt}</td><td class='num'>{t['kms']:g}</td><td class='num'>{t['rate_per_km']:g}</td>"
                            f"<td class='num'>—</td>")
                return (f"<td><b>{esc(t['travel_mode'])}</b><br><span class='note'>actual fare</span>{receipt}</td>"
                        f"<td class='num'>{t['kms']:g}</td><td class='num'>—</td><td class='num'>{t['fare']:,.0f}</td>")

            rows_html = "".join(f"""
                <tr>
                  <td>{t['claim_date']}</td><td>{t['emp_id']}</td><td>{esc(t['employee_name'])}</td>
                  <td>{esc(t['client_name'] or '')}</td><td>{esc(t['venue_particular'] or '')}</td>
                  {mode_cells(t)}
                  <td class="num">{t['parking']:,.0f}</td><td class="num">{t['other_charges']:,.0f}</td>
                  <td class="num"><strong>{t['total_amount']:,.0f}</strong></td><td>{t['payroll_month']}</td>
                  <td>{claim_status(t)}</td><td>{claim_charge(t)}</td><td>{claim_action(t)}</td>
                </tr>""" for t in trs)
            n_to_review = sum(1 for t in trs if t["status"] == "Submitted" and not cannot_decide(t["emp_id"])) if can_review else 0

            invoice_panel = ""
            if can_review:
                outstanding = chargeable_to_invoice(conn)
                inv_rows = "".join(f"""
                    <tr><td><strong>{esc(o['client_name'])}</strong></td><td>{pay_month_label(o['payroll_month'])}</td><td class="num">{o['n']}</td>
                      <td class="num"><strong>{o['amount']:,.0f}</strong></td><td style="white-space:normal; max-width:420px;">{esc(o['detail'])}</td>
                      <td><form method="POST" action="/mark_invoiced?ym={ym}" style="display:flex; gap:4px;">
                        <input type="hidden" name="client_name" value="{esc(o['client_name'], quote=True)}">
                        <input type="hidden" name="payroll_month" value="{o['payroll_month']}">
                        <input name="invoice_no" placeholder="Invoice no." size="10">
                        <button class="btn btn-ok" type="submit">Included in Invoice</button></form></td></tr>""" for o in outstanding)
                month_charge = conn.execute("""
                    SELECT t.*, s.employee_name FROM travel_claims t JOIN staff s ON s.emp_id = t.emp_id
                    WHERE t.payroll_month = ? AND t.status = 'Reviewed' AND t.chargeable = 1 ORDER BY t.client_name, t.claim_date
                """, (pm_str,)).fetchall()
                month_rows = "".join(f"""
                    <tr><td>{esc(t['client_name'])}</td><td>{t['claim_date']}</td><td>{esc(t['employee_name'])}</td><td>{esc(t['venue_particular'] or '')}</td>
                      <td class="num">{t['total_amount']:,.0f}</td>
                      <td>{f"<span class='badge b-ok'>Invoiced {esc(t['invoice_no'] or '')}</span>" if t['invoiced'] else "<span class='badge b-warn'>Not invoiced</span>"}</td></tr>"""
                    for t in month_charge)
                invoice_panel = f"""
            <div class="panel" id="chargeable">
              <h3>Chargeable Travel (Out-of-Pocket Expenses) — To Be Invoiced</h3>
              <p class="note">Chargeable claims paid in an <b>approved</b> payroll, not yet included in a client invoice. Mark each client's amount
                once it is in the invoice; until then this reminder pops up for the reviewer.</p>
              <table>
                <thead><tr><th>Client</th><th>Payroll Month</th><th class="num">Claims</th><th class="num">Amount (Rs.)</th><th>Details</th><th></th></tr></thead>
                <tbody>{inv_rows or "<tr><td colspan='6'>Nothing outstanding.</td></tr>"}</tbody>
              </table>
              <h4 style="margin:18px 0 8px;">All chargeable claims — {pay_month_label(pm_str)} payroll
                (Rs. {sum(t['total_amount'] for t in month_charge):,.0f})</h4>
              <table>
                <thead><tr><th>Client</th><th>Date</th><th>Staff</th><th>Venue</th><th class="num">Amount</th><th>Invoice</th></tr></thead>
                <tbody>{month_rows or "<tr><td colspan='6'>No chargeable claims in this month.</td></tr>"}</tbody>
              </table>
            </div>"""
            cfg_t = get_settings_dict(conn)
            base_km, base_price = float(cfg_t.get("travel_rate_base_km", 17)), float(cfg_t.get("travel_rate_base_price", 400))
            today_date, today_price = petrol_price_on(conn, date.today())
            prices = conn.execute("SELECT * FROM fuel_prices ORDER BY effective_date DESC LIMIT 15").fetchall()
            price_rows = "".join(
                f"<tr><td>{parse_date(p['effective_date']).strftime('%d %b %Y')}</td><td class='num'>Rs. {p['price']:g}</td>"
                f"<td class='num'><b>Rs. {km_rate_from_price(p['price'], cfg_t):g}</b></td><td>{p['source']}</td></tr>" for p in prices)
            max_up = float(cfg_t.get("travel_rate_max_uplift_pct", 10))
            uplift = rate_uplift_on(conn, date.today())
            formula_today = km_rate_from_price(today_price, cfg_t)
            uplifts = conn.execute("SELECT * FROM rate_uplifts ORDER BY id DESC LIMIT 10").fetchall()
            controls = ""
            if role in ROLE_ACTIONS["/set_travel_rate"]:  # approver: the formula and hand-entered prices
                controls += f"""
              <div style="display:flex; gap:24px; flex-wrap:wrap; margin:10px 0;">
                <form class="inline" method="POST" action="/set_travel_rate?ym={ym}" style="margin:0;">
                  <label>Formula: Rs. per km <input type="number" step="0.01" min="0.01" name="rate" value="{base_km:g}" required style="width:80px;"></label>
                  <label>at petrol Rs./litre <input type="number" step="0.01" min="1" name="base_price" value="{base_price:g}" required style="width:90px;"></label>
                  <button class="btn btn-sec" type="submit">Update Formula</button>
                </form>
                <form class="inline" method="POST" action="/add_fuel_price?ym={ym}" style="margin:0;">
                  <label>If the website is down — price from <input type="date" name="effective_date" required max="{date.today().isoformat()}"></label>
                  <label>Rs./litre <input type="number" step="0.01" min="1" name="price" required style="width:80px;"></label>
                  <button class="btn btn-sec" type="submit">Enter Price</button>
                </form>
              </div>"""
            if role in ROLE_ACTIONS["/set_rate_uplift"]:
                reviewer_limit = role == ROLE_REVIEWER
                controls += f"""
              <form class="inline" method="POST" action="/set_rate_uplift?ym={ym}" style="margin:10px 0;">
                <label>Increase over formula (%) <input type="number" step="0.5" min="0" {f'max="{max_up:g}"' if reviewer_limit else ''} name="pct"
                  value="{uplift['pct'] if uplift else 0:g}" required style="width:80px;"></label>
                {'' if reviewer_limit else f'<label>From <input type="date" name="effective_from" value="{date.today().isoformat()}"></label>'}
                <label>Reason (in writing) <input name="reason" required minlength="10" size="40" placeholder="e.g. heavy travel to out-of-city clients this month"></label>
                <button class="btn" type="submit">Save Increase</button>
              </form>
              <p class="note">{f"You may raise the rate by at most <b>{max_up:g}%</b> above the formula, from today, with a written reason; the approver is notified. 0% returns to the formula rate." if reviewer_limit else "As approver you may set any increase and start date."}</p>"""
            if role in ROLE_ACTIONS["/fetch_fuel_prices"]:
                controls += f"""
              <form method="POST" action="/fetch_fuel_prices?ym={ym}" style="margin:6px 0;"><button class="btn btn-sec" type="submit">Fetch Latest PSO Prices Now</button></form>
              <p class="note">Last automatic check: {html_lib.escape(cfg_t.get('fuel_price_last_check', 'not yet'))} (checked every 6 hours).
                Every formula change, increase or hand-entered price is recorded in the Approval Log.</p>"""
            uplift_rows = "".join(
                f"<tr><td>{parse_date(u['effective_from']).strftime('%d %b %Y')}</td><td class='num'>{u['pct']:g}%</td>"
                f"<td style='white-space:normal;'>{html_lib.escape(u['reason'])}</td><td>{html_lib.escape(u['set_by'] or '')}</td></tr>" for u in uplifts)
            rate_panel = f"""
            <div class="panel">
              <h3>Travelling Rate per Km — linked to the PSO petrol price</h3>
              <p class="note">The rate follows the <b>PSO Premier Euro 5</b> petrol price
                (<a href="{html_lib.escape(cfg_t.get('fuel_price_url', ''))}" target="_blank" rel="noopener">psopk.com</a>), read automatically:
                <b>rate per km = petrol price × {base_km:g} ÷ {base_price:g}</b> (Rs. {base_km:g}/km when petrol is Rs. {base_price:g}/litre).
                Each claim uses the price in force on the <b>date travelled</b>.</p>
              <div class="cards" style="margin-bottom:10px;">
                <div class="card"><h3>Petrol Today (Premier Euro 5)</h3><div class="val">{f"Rs. {today_price:g}" if today_price else "—"}</div>
                  <div class="sub">{f"effective {parse_date(today_date).strftime('%d %b %Y')}" if today_date else "no price on file yet"}</div></div>
                <div class="card"><h3>Formula Rate</h3><div class="val">Rs. {formula_today:g} / km</div>
                  <div class="sub">{f"{today_price:g} × {base_km:g} ÷ {base_price:g}" if today_price else "fallback rate (no petrol price yet)"}</div></div>
                <div class="card"><h3>Increase</h3><div class="val">{f"{uplift['pct']:g}%" if uplift and uplift['pct'] else "None"}</div>
                  <div class="sub">{html_lib.escape(f"{uplift['reason']} — {uplift['set_by']}") if uplift and uplift['pct'] else "rate = formula"}</div></div>
                <div class="card" style="border-left-color:#16a34a;"><h3>Travel Rate Today</h3><div class="val">Rs. {km_rate:g} / km</div>
                  <div class="sub">used for claims dated today</div></div>
              </div>
              {controls}
              <details><summary style="cursor:pointer;">Recent prices and rates</summary>
                <table style="margin-top:8px;"><thead><tr><th>Effective From</th><th class="num">Petrol Rs./Litre</th><th class="num">Formula Rs./Km</th><th>Source</th></tr></thead>
                  <tbody>{price_rows or "<tr><td colspan='4'>No prices yet.</td></tr>"}</tbody></table>
                {f'<h4 style="margin:14px 0 6px;">Increases over the formula</h4><table><thead><tr><th>From</th><th class="num">Increase</th><th>Reason</th><th>By</th></tr></thead><tbody>{uplift_rows}</tbody></table>' if uplifts else ''}</details>
            </div>"""
            body = rate_panel + f"""
            <div class="panel">
              <h3>Submit Travel Claim (25th cut-off rule)</h3>
              <p class="note">{travel_note}<br>
                <b>Own vehicle</b>: kms × the rate on the date travelled. <b>InDrive, Yango, cab, auto rickshaw or bike ride service</b>:
                the <b>actual fare</b> is reimbursed once the reviewer approves it — attach the receipt or app screenshot if you have it.
                Parking and other charges can be added to either.</p>
              <form class="inline" method="POST" action="/add_travel?ym={ym}" enctype="multipart/form-data">
                <label>Date <input type="date" name="claim_date" required max="{date.today().isoformat()}"></label>
                {travel_who}
                <label>Client <input name="client_name" placeholder="Client / IPAC / FBR"></label>
                <label>Venue <input name="venue_particular" placeholder="Visit details"></label>
                <label>Travelled By <select name="travel_mode">{"".join(f"<option>{m}</option>" for m in TRAVEL_MODES)}</select></label>
                <label>Kms (own vehicle) <input type="number" step="0.1" min="0" name="kms" value="0" style="width:75px;"></label>
                <label>Actual Fare (ride service) <input type="number" min="0" name="fare" value="0" style="width:90px;"></label>
                <label>Receipt / Screenshot <input type="file" name="receipt" accept=".pdf,.jpg,.jpeg,.png"></label>
                <label>Parking <input type="number" min="0" name="parking" value="0" style="width:75px;"></label>
                <label>Other <input type="number" min="0" name="other_charges" value="0" style="width:75px;"></label>
                <button class="btn" type="submit">Submit Claim</button>
              </form>
            </div>
            <div class="panel">
              <h3>Travel Claims{f" — <span class='badge b-warn'>{n_to_review} waiting for your review</span>" if n_to_review else ""}</h3>
              <table>
                <thead><tr><th>Date</th><th>ID</th><th>Name</th><th>Client</th><th>Venue</th><th>Travelled By</th><th class="num">Kms</th><th class="num">Rate</th><th class="num">Fare</th><th class="num">Parking</th><th class="num">Other</th><th class="num">Total</th><th>Payroll Month</th><th>Status</th><th>Chargeable</th><th></th></tr></thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>""" + invoice_panel

        elif tab == "loans":
            esc = html_lib.escape
            cfg = get_settings_dict(conn)
            only_emp = (own_emp or "") if is_staff else None  # Staff see only their own records
            max_sal, max_inst = float(cfg.get("loan_max_salaries", 2)), int(float(cfg.get("loan_max_instalments", 20)))
            lns = conn.execute("""SELECT a.*, s.employee_name FROM advances_loans a JOIN staff s ON a.emp_id=s.emp_id
                                  WHERE ? IS NULL OR a.emp_id = ? ORDER BY a.loan_ref""", (only_emp, only_emp)).fetchall()
            rows_html = "".join(f"""
                <tr>
                  <td>{l['loan_ref']}</td><td>{l['emp_id']}</td><td>{esc(l['employee_name'])}</td>
                  <td>{l['loan_type']}</td><td>{l['date_given']}</td><td class="num">{l['amount']:,.0f}</td>
                  <td class="num">{l['monthly_instalment']:,.0f}</td><td>{l['first_recovery_month']}</td>
                  <td>{'Yes' if l['hold_this_month'] else 'No'}</td><td>{esc(l['purpose'] or '')}</td><td>{esc(l['remarks'] or '')}</td>
                  <td>{"<span class='badge b-warn'>Awaiting payment</span>" if not l['disbursed'] else
                       f"<span class='badge b-ok'>Paid {(l['disbursed_at'] or '')[:10]}</span><br><span class='note'>{l['disbursement_batch']}</span>" if l['disbursement_batch'] else "Paid"}</td>
                </tr>""" for l in lns)
            loans_head = ("<thead><tr><th>Ref</th><th>ID</th><th>Name</th><th>Type</th><th>Date</th><th class='num'>Amount</th><th class='num'>Instalment</th>"
                          "<th>1st Rec.</th><th>Hold?</th><th>Purpose</th><th>Remarks</th><th>Paid</th></tr></thead>")
            pay_panel = ""
            if role in ROLE_ACTIONS["/export_loans"]:
                waiting = loans_to_disburse(conn)
                last = json.loads(cfg["last_loan_batch"]) if cfg.get("last_loan_batch") else None
                last_html = ""
                if last:
                    files = " and ".join(f"<code>{esc(p)}</code>" for p in (last["ibft"], last["ift"]) if p)
                    last_html = (f"<p class='note'>Last batch <b>{last['batch']}</b> by {esc(last['by'])} on {last['at'][:16].replace('T', ' ')}: "
                                 f"{len(last['paid'])} loan(s) — files {files}."
                                 + (f"<br><b>Left out (no bank account / IBFT code):</b> {esc(', '.join(last['skipped']))}" if last["skipped"] else "") + "</p>")
                wait_rows = "".join(
                    f"<tr><td>{w['loan_ref']}</td><td>{w['emp_id']} {esc(w['employee_name'])}</td><td>{w['loan_type']}</td><td class='num'>{w['amount']:,.0f}</td>"
                    f"<td>{w['approval_date'] or ''}</td><td>{esc(w['bank_name'] or '')}</td>"
                    f"<td>{'Meezan IFT' if w['is_meezan'] == 1 or w['bank_name'] == 'Meezan Bank' else 'IBFT' if w['ibft_bank_code'] else '<span class=\"badge b-warn\">No IBFT bank code</span>'}"
                    f"{'' if w['bank_account_iban'] else ' <span class=\"badge b-warn\">No bank account</span>'}</td></tr>" for w in waiting)
                pay_panel = f"""
            <div class="panel">
              <h3>Approved Loans Awaiting Payment</h3>
              <p class="note">Loans and advances are <b>paid by separate IBFT / Meezan IFT files</b>, never with salaries. Generating the files
                marks the loans in them as paid (with the batch number), so a loan can't be paid twice. Recovery from salary starts the month
                after approval.</p>
              <table><thead><tr><th>Ref</th><th>Staff</th><th>Type</th><th class="num">Amount</th><th>Approved</th><th>Bank</th><th>Paid By</th></tr></thead>
                <tbody>{wait_rows or "<tr><td colspan='7'>Nothing waiting to be paid.</td></tr>"}</tbody></table>
              <form method="POST" action="/export_loans?ym={ym}" style="margin-top:12px;">
                <button class="btn btn-ok" type="submit"{'' if waiting else ' disabled'}>Generate Loan Bank Files (IBFT / IFT) — Rs. {sum(w['amount'] for w in waiting):,.0f}</button>
              </form>
              {last_html}
            </div>"""
            apps = conn.execute("""SELECT a.*, s.employee_name FROM loan_applications a JOIN staff s ON s.emp_id = a.emp_id
                                   WHERE ? IS NULL OR a.emp_id = ? ORDER BY a.id DESC LIMIT 200""", (only_emp, only_emp)).fetchall()
            reviewer_emps = approver_only

            def app_status(a):
                st = a["status"]
                out = f"<span class='badge {'b-ok' if st == 'Approved' else 'b-warn'}'>{st}</span>"
                if a["reviewed_by"]:
                    out += f"<br><span class='note'>{'Recommended' if st != 'Rejected' or a['decided_by'] else 'Rejected'} by {esc(a['reviewed_by'])}</span>"
                if a["review_note"]:
                    out += f"<br><span class='note'>“{esc(a['review_note'])}”</span>"
                if a["decided_by"]:
                    approved = f" — Rs. {a['approved_amount']:,.0f} as {a['loan_ref']}" if st == "Approved" else ""
                    out += f"<br><span class='note'>{st} by {esc(a['decided_by'])}{approved}</span>"
                return out

            def app_action(a):
                st = a["status"]
                if st == "Applied" and a["emp_id"] == own_emp:
                    return (f"<form method='POST' action='/withdraw_loan?ym={ym}'><input type='hidden' name='id' value='{a['id']}'>"
                            f"<button class='btn btn-sec' type='submit'>Withdraw</button></form>")
                if cannot_decide(a["emp_id"]):
                    return ""
                if role == ROLE_REVIEWER and st == "Applied":
                    exc = ("<label style='flex-direction:row; gap:4px; align-items:center;'><input type='checkbox' name='exception' value='1'>"
                           "Recommend as exception</label>") if a["exceeds_policy"] else ""
                    return (f"<form method='POST' action='/review_loan?ym={ym}' style='display:flex; gap:6px; flex-wrap:wrap; align-items:center;'>"
                            f"<input type='hidden' name='id' value='{a['id']}'>{exc}"
                            f"<input name='note' size='22' placeholder='{'Justification (required)' if a['exceeds_policy'] else 'Note / reason'}'>"
                            f"<button class='btn btn-ok' type='submit' name='decision' value='Recommended'>Recommend</button>"
                            f"<button class='btn btn-danger' type='submit' name='decision' value='Rejected'>Reject</button></form>")
                if role == ROLE_APPROVER and (st == "Recommended" or (st == "Applied" and a["emp_id"] in reviewer_emps)):
                    return (f"<form method='POST' action='/decide_loan?ym={ym}' style='display:flex; gap:6px; flex-wrap:wrap; align-items:center;'>"
                            f"<input type='hidden' name='id' value='{a['id']}'>"
                            f"<label>Approve Rs. <input type='number' name='approved_amount' value='{a['amount']:.0f}' max='{a['amount']:.0f}' min='1' style='width:95px;'></label>"
                            f"<input name='note' size='16' placeholder='Note'>"
                            f"<button class='btn btn-ok' type='submit' name='decision' value='Approved'>Approve</button>"
                            f"<button class='btn btn-danger' type='submit' name='decision' value='Rejected'>Reject</button></form>")
                return ""

            def policy_cell(a):
                avail = a["policy_limit"] - a["outstanding"]
                if a["exceeds_policy"]:
                    return (f"<span class='badge b-warn'>Exceeds policy</span><br><span class='note'>limit Rs. {a['policy_limit']:,.0f}, "
                            f"already Rs. {a['outstanding']:,.0f}; additional Rs. {a['amount'] - max(0, avail):,.0f}</span>")
                return f"<span class='badge b-ok'>Within policy</span><br><span class='note'>limit Rs. {a['policy_limit']:,.0f}</span>"

            app_rows = "".join(f"""
                <tr><td>#{a['id']}</td><td>{a['applied_at'][:10]}</td><td>{a['emp_id']} {esc(a['employee_name'])}</td><td>{a['loan_type']}</td>
                  <td class="num"><strong>{a['amount']:,.0f}</strong></td>
                  <td class="num">{a['instalments'] if a['loan_type'] == 'Loan' else '1 (next month)'}</td>
                  <td>{esc(a['purpose'] or '')}</td><td>{policy_cell(a)}</td><td>{app_status(a)}</td><td>{app_action(a)}</td></tr>""" for a in apps)

            apply_panel = ""
            if own_emp:
                me = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (own_emp,)).fetchone()
                if me is not None and me["staff_type"] != EOBI_ONLY_TYPE:
                    my_limit = effective_salary(conn, me, pm_str, cfg) * max_sal
                    my_out = loan_outstanding(conn, own_emp, pm_str)
                    apply_panel = f"""
            <div class="panel">
              <h3>Apply for a Loan or Advance</h3>
              <p class="note">Policy: loans and advances outstanding may not exceed <b>{max_sal:g} months' salary</b> (your limit:
                <b>Rs. {my_limit:,.0f}</b>; already outstanding or applied for: <b>Rs. {my_out:,.0f}</b>; available:
                <b>Rs. {max(0, my_limit - my_out):,.0f}</b>). A loan is repaid in at most <b>{max_inst} monthly instalments</b>; an advance is
                recovered in full from next month's salary. Every application is reviewed by the reviewer and approved by the approver.
                An amount above the limit is possible only if the reviewer recommends it as an exception and the approver approves it.</p>
              <form class="inline" method="POST" action="/apply_loan?ym={ym}">
                <label>Type <select name="loan_type"><option>Loan</option><option>Advance</option></select></label>
                <label>Amount (Rs.) <input type="number" name="amount" min="1" required style="width:110px;"></label>
                <label>Instalments (loan) <input type="number" name="instalments" min="1" max="{max_inst}" value="{max_inst}" style="width:80px;"></label>
                <label>Purpose <input name="purpose" required size="30" placeholder="Reason for the loan"></label>
                <button class="btn" type="submit">Apply</button>
              </form>
            </div>"""
            n_waiting = sum(1 for a in apps if app_action(a) and a["emp_id"] != own_emp)
            apps_panel = f"""
            <div class="panel">
              <h3>{'My Loan Applications' if is_staff else 'Loan &amp; Advance Applications'}{f" — <span class='badge b-warn'>{n_waiting} waiting for you</span>" if n_waiting else ""}</h3>
              <table><thead><tr><th>#</th><th>Applied</th><th>Staff</th><th>Type</th><th class="num">Amount</th><th class="num">Instalments</th><th>Purpose</th><th>Policy</th><th>Status</th><th></th></tr></thead>
                <tbody>{app_rows or "<tr><td colspan='10'>No applications yet.</td></tr>"}</tbody></table>
            </div>"""
            loans_table = f"""
            <div class="panel">
              <h3>{'My Loans &amp; Advances' if is_staff else 'Loans &amp; Advances'}</h3>
              <table>
                {loans_head}
                <tbody>{rows_html or "<tr><td colspan='12'>None.</td></tr>"}</tbody>
              </table>
            </div>"""
            if is_staff:
                body = apply_panel + apps_panel + loans_table
            else:
                body = apply_panel + apps_panel + pay_panel + f"""
            <div class="panel">
              <h3>Add / Update Advance or Loan Directly (e.g. earlier loans)</h3>
              <p class="note">{'Saved immediately and recorded in the approval log.' if is_approver else '<b>Sent to the approver; recovery starts only after approval.</b>' if role == ROLE_PREPARER else '<b>View only.</b>'}
                At most {max_inst} instalments; an amount above {max_sal:g} salaries is flagged for the approver.</p>
              <form class="inline" method="POST" action="/add_loan?ym={ym}">
                <label>Ref <input name="loan_ref" placeholder="L007" required size="6"></label>
                <label>Emp ID <input name="emp_id" placeholder="E001" required size="6"></label>
                <label>Type <select name="loan_type"><option>Loan</option><option>Advance</option></select></label>
                <label>Date Given <input type="date" name="date_given" required></label>
                <label>Amount <input type="number" name="amount" required style="width:100px;"></label>
                <label>Monthly Inst. <input type="number" name="monthly_instalment" value="0" style="width:100px;"></label>
                <label>Purpose <input name="purpose" placeholder="Purpose"></label>
                <button class="btn" type="submit">Save Loan</button>
              </form>
              <table>
                {loans_head}
                <tbody>{rows_html}</tbody>
              </table>
            </div>"""

        elif tab == "inputs":
            mis = conn.execute("SELECT m.*, s.employee_name FROM monthly_inputs m JOIN staff s ON m.emp_id=s.emp_id WHERE m.payroll_month=? ORDER BY m.emp_id", (pm_str,)).fetchall()
            rows_html = "".join(f"""
                <tr>
                  <td>{m['emp_id']}</td><td>{m['employee_name']}</td><td class="num">{m['bonus']:,.0f}</td>
                  <td class="num">{m['arrears_other']:,.0f}</td>
                  <td class="num">{m['partial_days_client_deduction']:,.0f}</td><td class="num">{m['leave_days_waived']}</td><td>{m['note'] or ''}</td>
                </tr>""" for m in mis)
            body = f"""
            <div class="panel">
              <h3>Monthly Adjustments for {ym} (Bonus, Arrears, Client Deductions, Waived Leaves)</h3>
              <p class="note">{'Saved immediately and recorded in the approval log.' if is_approver else '<b>Sent to the approver; the adjustment counts only after approval.</b>' if role == ROLE_PREPARER else '<b>View only.</b>'}</p>
              <form class="inline" method="POST" action="/add_input?ym={ym}">
                <label>Emp ID <input name="emp_id" placeholder="E001" required size="6"></label>
                <label>Bonus <input type="number" name="bonus" value="0" style="width:85px;"></label>
                <label>Arrears <input type="number" name="arrears_other" value="0" style="width:85px;"></label>
                <label>Client Ded. <input type="number" name="partial_days_client_deduction" value="0" style="width:85px;"></label>
                <label>Waived Leaves <input type="number" step="0.5" name="leave_days_waived" value="0" style="width:85px;"></label>
                <label>Note <input name="note" placeholder="Approved by partner"></label>
                <button class="btn" type="submit">Save Input</button>
              </form>
              <table>
                <thead><tr><th>ID</th><th>Name</th><th class="num">Bonus</th><th class="num">Arrears</th><th class="num">Client Ded.</th><th class="num">Waived Lvs</th><th>Note</th></tr></thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>"""

        elif tab == "approvals":
            pending = conn.execute("SELECT * FROM change_requests WHERE status = 'Pending' ORDER BY id").fetchall()
            history = conn.execute("SELECT * FROM change_requests WHERE status != 'Pending' ORDER BY id DESC LIMIT 100").fetchall()

            def decision_buttons(r):
                if not is_approver:
                    return "<span class='badge b-warn'>Awaiting approver</span>"
                return (f"<div style='display:flex; gap:6px;'>"
                        f"<form method='POST' action='/approve_request?ym={ym}'><input type='hidden' name='id' value='{r['id']}'><button class='btn btn-ok' type='submit'>Approve</button></form>"
                        f"<form method='POST' action='/reject_request?ym={ym}'><input type='hidden' name='id' value='{r['id']}'><button class='btn btn-danger' type='submit'>Reject</button></form></div>")

            type_names = {t["code"]: t["name"] for t in conn.execute("SELECT code, name FROM doc_types")}

            def request_docs(r):
                if r["kind"] != "add_staff":
                    return ""
                docs = conn.execute("SELECT id, doc_type FROM staff_documents WHERE change_request_id = ?", (r["id"],)).fetchall()
                return ("<br><span class='note'>Documents: " + (", ".join(
                    f"<a href='/staff_doc?id={d['id']}' target='_blank'>{html_lib.escape(type_names.get(d['doc_type'], d['doc_type']))}</a>" for d in docs)
                    or "none") + "</span>")

            pending_rows = "".join(f"""
                <tr>
                  <td>#{r['id']}</td><td>{r['requested_at'].replace('T', ' ')}</td><td>{html_lib.escape(r['requested_by'])}</td>
                  <td>{KIND_LABELS.get(r['kind'], r['kind'])}</td><td style="white-space:normal;">{html_lib.escape(r['summary'])}{request_docs(r)}</td>
                  <td>{decision_buttons(r)}</td>
                </tr>""" for r in pending) or "<tr><td colspan='6'><em>No changes waiting for approval.</em></td></tr>"
            history_rows = "".join(f"""
                <tr>
                  <td>#{r['id']}</td><td>{r['requested_at'].replace('T', ' ')}</td><td>{html_lib.escape(r['requested_by'])}</td>
                  <td>{KIND_LABELS.get(r['kind'], r['kind'])}</td><td style="white-space:normal;">{html_lib.escape(r['summary'])}</td>
                  <td><span class="badge {'b-ok' if r['status'] == 'Approved' else 'b-warn'}">{r['status']}</span></td>
                  <td>{html_lib.escape(r['decided_by'] or '')}</td><td>{(r['decided_at'] or '').replace('T', ' ')}</td>
                </tr>""" for r in history) or "<tr><td colspan='8'><em>No decisions yet.</em></td></tr>"
            waiting_credits = [c for c in conn.execute("""SELECT c.*, s.employee_name FROM leave_credits c JOIN staff s ON s.emp_id = c.emp_id
                                                          WHERE c.status IN ('Recommended', 'Applied', 'Late') AND c.source != 'Timesheet' ORDER BY c.id""").fetchall()
                               if c["status"] == "Recommended" or c["emp_id"] in approver_only]
            credit_btns = lambda c: ("<span class='badge b-warn'>Awaiting approver</span>" if not is_approver else
                                     credit_review_form(c, ym, "Condone &amp; Approve" if c["status"] == "Late" else "Approve") if c["status"] != "Recommended" else
                                     "<div style='display:flex; gap:6px;'>" + "".join(
                                         f"<form method='POST' action='/decide_credit?ym={ym}'><input type='hidden' name='id' value='{c['id']}'>"
                                         f"<input type='hidden' name='decision' value='{d}'><button class='btn {css}' type='submit'>{label}</button></form>"
                                         for d, label, css in (("Approved", "Approve", "btn-ok"), ("Rejected", "Reject", "btn-danger"))) + "</div>")
            credits_panel = f"""
            <div class="panel">
              <h3>Leave Credits Waiting for Approval</h3>
              <p class="note">Recommended by the reviewer for working on a gazetted holiday or being called in on a weekend. A credit adds
                to the person's leave only after approval.</p>
              <table><thead><tr><th>Recommended</th><th>By</th><th>Staff</th><th>Date Worked</th><th class="num">Days</th><th>Reason</th><th>Decision</th></tr></thead>
                <tbody>{"".join(f"<tr><td>{c['requested_at'][:16].replace('T', ' ')}</td><td>{html_lib.escape(c['requested_by'] or '')}</td><td>{c['emp_id']} {html_lib.escape(c['employee_name'])}</td><td>{parse_date(c['work_date']).strftime('%a %d %b %Y')}</td><td class='num'>{c['days']:g}</td><td>{CREDIT_REASONS.get(c['reason'], c['reason'])}{('<br><span class=note>' + html_lib.escape(c['note']) + '</span>') if c['note'] else ''}</td><td>{credit_btns(c)}</td></tr>" for c in waiting_credits)}</tbody></table>
            </div>""" if waiting_credits else ""
            waiting_loans = [a for a in conn.execute("""SELECT a.*, s.employee_name FROM loan_applications a JOIN staff s ON s.emp_id = a.emp_id
                                                        WHERE a.status IN ('Applied', 'Recommended') ORDER BY a.id""").fetchall()
                             if a["status"] == "Recommended" or a["emp_id"] in approver_only]
            loans_panel = f"""
            <div class="panel">
              <h3>Loan / Advance Applications Waiting for Approval</h3>
              <table><thead><tr><th>#</th><th>Staff</th><th>Type</th><th class="num">Amount</th><th>Policy</th><th>Recommended By</th><th></th></tr></thead>
                <tbody>{"".join(f"<tr><td>#{a['id']}</td><td>{a['emp_id']} {html_lib.escape(a['employee_name'])}</td><td>{a['loan_type']}</td><td class='num'>{a['amount']:,.0f}</td><td>{'<span class=\"badge b-warn\">Exceeds policy — exception</span>' if a['exceeds_policy'] else '<span class=\"badge b-ok\">Within policy</span>'}</td><td>{html_lib.escape(a['reviewed_by'] or '(goes straight to approver)')}</td><td><a class='btn btn-sec' href='/?tab=loans&ym={ym}'>Decide on Loans tab</a></td></tr>" for a in waiting_loans)}</tbody></table>
            </div>""" if waiting_loans else ""
            body = credits_panel + loans_panel + f"""
            <div class="panel">
              <h3>Waiting for Approval</h3>
              <p class="note">New staff members, staff / pay changes, monthly adjustments and loans entered by a Preparer
                change nothing until the approver approves them here. Each month's payroll must also be approved on the
                Monthly Payroll tab before bank files can be exported; an approved month is locked.</p>
              <table>
                <thead><tr><th>Ref</th><th>Requested</th><th>By</th><th>Type</th><th>Change</th><th>Decision</th></tr></thead>
                <tbody>{pending_rows}</tbody>
              </table>
            </div>
            <div class="panel">
              <h3>Approval Log</h3>
              <table>
                <thead><tr><th>Ref</th><th>Requested</th><th>By</th><th>Type</th><th>Change</th><th>Status</th><th>Decided By</th><th>Decided</th></tr></thead>
                <tbody>{history_rows}</tbody>
              </table>
            </div>"""

        elif tab == "users":
            def access_badge(u):
                if u["emp_id"] is None or u["employee_name"] is None:
                    return ""
                if staff_access_ended(u):
                    return " <span class='badge b-warn'>Access removed (left the firm)</span>"
                return f" <span class='badge b-warn'>Access ends after {u['leaving_date']}</span>" if u["leaving_date"] else ""

            users = conn.execute("""SELECT u.username, u.role, u.emp_id, u.created_at, u.must_change_pw, s.employee_name, s.status, s.leaving_date FROM users u
                                    LEFT JOIN staff s ON s.emp_id = u.emp_id
                                    ORDER BY CASE u.role WHEN 'Approver' THEN 0 WHEN 'Reviewer' THEN 1 WHEN 'Preparer' THEN 2 ELSE 3 END, u.username""").fetchall()
            rows_html = "".join(f"""
                <tr>
                  <td><strong>{html_lib.escape(u['username'])}</strong></td><td>{u['role']}</td>
                  <td>{(u['emp_id'] or '') + (' — ' + u['employee_name'] if u['employee_name'] else '')}{access_badge(u)}</td><td>{u['created_at'].replace('T', ' ')}</td>
                  <td>{"<span class='badge b-warn'>Starting password not changed yet</span>" if u['must_change_pw'] else "<span class='badge b-ok'>Own password set</span>"}</td>
                  <td>{'' if u['username'] == user else f"<form method='POST' action='/delete_user?ym={ym}'><input type='hidden' name='username' value='{html_lib.escape(u['username'])}'><button class='btn btn-danger' type='submit'>Remove</button></form>"}</td>
                </tr>""" for u in users)
            role_options = "".join(f'<option value="{r}"{" selected" if r == ROLE_STAFF else ""}>{r}</option>' for r in ROLES)
            body = f"""
            <div class="panel">
              <h3>Users</h3>
              <p class="note">
                <b>Approver</b> — approves staff / pay changes and each month's payroll; own changes apply at once.<br>
                <b>Reviewer</b> — sees everything; approves or rejects leave applications; reviews the prepared payroll.<br>
                <b>Preparer</b> — enters data and prepares the payroll; new staff, pay changes, adjustments and loans wait for approval.<br>
                <b>Staff</b> — can only apply for their own leave and submit their own travel claims (must be linked to an Emp ID).<br>
                Give each person a starting password; they must change it to their own the first time they log in.
              </p>
              <form class="inline" method="POST" action="/add_user?ym={ym}">
                <label>Username <input name="username" required autocomplete="off"></label>
                <label>Temporary Password <input type="password" name="password" required minlength="{MIN_PASSWORD_LEN}" autocomplete="new-password"></label>
                <label>Role <select name="role">{role_options}</select></label>
                <label>Emp ID <input name="emp_id" placeholder="E003" size="6"></label>
                <button class="btn" type="submit">Add User</button>
              </form>
              <table>
                <thead><tr><th>Username</th><th>Role</th><th>Staff Member</th><th>Created</th><th>Password</th><th></th></tr></thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>"""
            delegates = conn.execute("""SELECT d.*, s.employee_name, u.role FROM portal_delegates d JOIN staff s ON s.emp_id = d.emp_id
                                        LEFT JOIN users u ON u.username = d.delegate_username ORDER BY d.emp_id""").fetchall()
            staff_logins = conn.execute("""SELECT u.emp_id, s.employee_name FROM users u JOIN staff s ON s.emp_id = u.emp_id
                                           WHERE u.role = ? ORDER BY u.emp_id""", (ROLE_STAFF,)).fetchall()
            officers = [u for u in users if u["role"] != ROLE_STAFF]
            del_rows = "".join(
                f"<tr><td>{d['emp_id']} {html_lib.escape(d['employee_name'])}</td><td>{html_lib.escape(d['delegate_username'])} ({d['role'] or '?'})</td>"
                f"<td>{html_lib.escape(d['granted_by'] or '')} {(d['granted_at'] or '')[:10]}</td>"
                f"<td><form method='POST' action='/remove_delegate?ym={ym}'><input type='hidden' name='emp_id' value='{d['emp_id']}'>"
                f"<button class='btn btn-danger' type='submit'>Remove</button></form></td></tr>" for d in delegates)
            body += f"""
            <div class="panel">
              <h3>Portals Operated for Staff</h3>
              <p class="note">For staff who can't use the portal themselves. The operator switches into the staff member's portal (leave,
                travel, loans, timesheet) from their own login; everything is recorded as done by the operator for that person. The reviewer
                never reviews what he files for them: those requests go straight to the approver.</p>
              <form class="inline" method="POST" action="/set_delegate?ym={ym}">
                <label>Staff Member <select name="emp_id">{"".join(f"<option value='{s['emp_id']}'>{s['emp_id']} — {html_lib.escape(s['employee_name'])}</option>" for s in staff_logins)}</select></label>
                <label>Operated By <select name="delegate_username">{"".join(f"<option value='{html_lib.escape(o['username'])}'>{html_lib.escape(o['username'])} — {o['role']}{(' (' + html_lib.escape(o['employee_name']) + ')') if o['employee_name'] else ''}</option>" for o in officers)}</select></label>
                <button class="btn" type="submit">Assign</button>
              </form>
              <table><thead><tr><th>Staff Member</th><th>Operated By</th><th>Assigned</th><th></th></tr></thead>
                <tbody>{del_rows or "<tr><td colspan='4'>None.</td></tr>"}</tbody></table>
            </div>"""

        elif tab == "account" and acting:
            body = f"""<div class="panel" style="max-width:520px;"><h3>My Account</h3>
              <p class="note">You are operating <b>{html_lib.escape(acting)}</b>'s portal. Their password can't be changed from here.</p></div>"""

        elif tab == "account":
            linked = conn.execute("SELECT employee_name FROM staff WHERE emp_id = ?", (own_emp,)).fetchone() if own_emp else None
            body = f"""
            <div class="panel" style="max-width:520px;">
              <h3>My Account</h3>
              <p class="note">Signed in as <b>{html_lib.escape(user)}</b> — {role}{f" — {own_emp} {html_lib.escape(linked['employee_name'])}" if linked else ""}</p>
              {f"<div class='flash flash-warn'>{FLASH_MESSAGES['pw_must_change'][1]}</div>" if account['must_change_pw'] and not flash_text else ""}
              <form method="POST" action="/change_password?ym={ym}" style="display:flex; flex-direction:column; gap:12px; max-width:300px;">
                <label>Current Password <input type="password" name="current" required autocomplete="current-password"></label>
                <label>New Password <input type="password" name="new" required minlength="{MIN_PASSWORD_LEN}" autocomplete="new-password"></label>
                <label>Confirm New Password <input type="password" name="confirm" required autocomplete="new-password"></label>
                <button class="btn" type="submit">Change Password</button>
              </form>
            </div>"""

        popup = ""
        if role == ROLE_REVIEWER:  # reminder to bill clients for chargeable travel once the payroll is approved
            outstanding = chargeable_to_invoice(conn)
            if outstanding:
                total = sum(o["amount"] for o in outstanding)
                key = f"ope-{len(outstanding)}-{total:.0f}"
                items = "".join(f"<tr><td>{html_lib.escape(o['client_name'])}</td><td>{pay_month_label(o['payroll_month'])}</td>"
                                f"<td class='num'>{o['amount']:,.0f}</td></tr>" for o in outstanding)
                popup = f"""
<div id="ope-popup" class="modal-bg"><div class="modal">
  <h3 style="margin-top:0;">Reminder: include chargeable travel in client invoices</h3>
  <p class="note">These out-of-pocket expenses were paid in an approved payroll and are chargeable to the clients below.
    Add them to the invoices, then mark each one on the Travel Claims tab.</p>
  <table><thead><tr><th>Client</th><th>Payroll Month</th><th class="num">Amount (Rs.)</th></tr></thead><tbody>{items}</tbody>
    <tfoot><tr><th colspan="2">Total</th><th class="num">{total:,.0f}</th></tr></tfoot></table>
  <div style="display:flex; gap:8px; margin-top:14px; justify-content:flex-end;">
    <button class="btn btn-sec" type="button" onclick="dismissOpe()">Remind me later</button>
    <a class="btn btn-ok" href="/?tab=travel&ym={ym}#chargeable">Open Chargeable Report</a>
  </div>
</div></div>
<script>
  function dismissOpe() {{ try {{ sessionStorage.setItem("{key}", "1"); }} catch (e) {{}} document.getElementById("ope-popup").remove(); }}
  try {{ if (sessionStorage.getItem("{key}") || location.hash === "#chargeable") document.getElementById("ope-popup").remove(); }} catch (e) {{}}
</script>"""
        claims_to_review = (sum(1 for r in conn.execute("SELECT emp_id FROM travel_claims WHERE status = 'Submitted'") if not cannot_decide(r["emp_id"]))
                            if role in ROLE_ACTIONS["/review_travel"] else 0)
        conn.close()
        tab_labels = {"payroll": "Monthly Payroll", "staff": "Staff Master",
                      "leaves": f"Leave Register{f' ({credits_for_review})' if role == ROLE_REVIEWER and credits_for_review else ''}", "holidays": "Gazetted Holidays", "timesheets": "Timesheets", "email": "Email", "attendance": "Attendance",
                      "travel": f"Travel Claims (Rs. {km_rate:g}/km){f' ({claims_to_review})' if claims_to_review else ''}",
                      "loans": f"Advances & Loans{f' ({loans_waiting})' if loans_waiting else ''}", "inputs": "Monthly Inputs",
                      "approvals": f"Approvals{f' ({pending_count})' if pending_count else ''}", "users": "Users", "account": "My Account"}
        nav_links = "\n  ".join(f'<a href="/?tab={t}&ym={ym}" class="{"active" if t == tab else ""}">{tab_labels[t]}</a>' for t in allowed_tabs)
        header_actions = ""
        if role in (ROLE_APPROVER, ROLE_PREPARER):
            header_actions += f'<a class="btn" href="/recalc?ym={ym}&tab={tab}">Recalculate</a>'
        if not is_staff:
            header_actions += f' <a class="btn btn-sec" href="/export?ym={ym}">Export Bank CSVs</a>'
        html = HTML_TEMPLATE.format(
            firm_name=html_lib.escape(FIRM_NAME), firm_short=html_lib.escape(FIRM_SHORT), tab=tab, ym=ym, body=body, user=html_lib.escape(user), role=role, km_rate=f"{km_rate:g}",
            flash=portal_bar + (f'<div class="flash flash-{flash_style}">{flash_text}</div>' if flash_text else ""),
            nav_links=nav_links, header_actions=header_actions, popup=popup,
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_POST(self):
        parsed = urlparse(self.path)
        data = self.read_form()  # read the body before any early redirect, or the client sees a reset connection
        if parsed.path in ("/login", "/setup", "/logout"):
            return self.handle_auth_post(parsed.path, data)
        real_user = self.current_user()
        if not real_user:
            return self.redirect("/login")
        if parsed.path in ("/act_as", "/stop_acting"):  # switch into / out of a portal delegated to this user
            emp_id = data.get("emp_id", "").strip().upper()
            conn = get_conn()
            allowed = parsed.path == "/stop_acting" or any(p["emp_id"] == emp_id for p in delegated_portals(conn, real_user))
            conn.close()
            if not allowed:
                return self.redirect("/?msg=not_allowed")
            set_session_acting(self.session_token(), emp_id if parsed.path == "/act_as" else None)
            return self.redirect("/?tab=leaves" if parsed.path == "/act_as" else "/")
        account, user, acting = self.effective_account(real_user)
        role = account["role"]
        if acting and parsed.path == "/change_password":
            return self.redirect("/?tab=account&msg=not_allowed")

        qs = parse_qs(parsed.query)
        ym = qs.get("ym", ["2026-11"])[0]
        if len(ym) != 7 or not parse_date(f"{ym}-01"):
            ym = "2026-11"
        pm_str = f"{ym}-01"
        emp_param = "".join(c for c in qs.get("emp", [""])[0].upper() if c.isalnum())
        if emp_param:
            data["emp_id"] = emp_param

        next_tab = {"/upload_emails": "email", "/set_email": "email", "/save_mail_account": "email", "/send_test_email": "email",
                    "/send_logins": "email", "/set_app_url": "email", "/set_approver_email": "email",
                    "/add_holiday": "holidays", "/delete_holiday": "holidays", "/email_holidays": "holidays",
                    "/add_credit": "leaves", "/decide_credit": "leaves", "/apply_credit": "leaves", "/review_credit": "leaves",
                    "/apply_loan": "loans", "/withdraw_loan": "loans", "/review_loan": "loans", "/decide_loan": "loans", "/export_loans": "loans",
                    "/save_timesheet": "timesheets", "/decide_timesheet": "timesheets", "/save_ts_code": "timesheets", "/set_ts_access": "timesheets",
                    "/review_travel": "travel", "/withdraw_travel": "travel", "/mark_invoiced": "travel",
                    "/fetch_fuel_prices": "travel", "/add_fuel_price": "travel", "/set_rate_uplift": "travel",
                    "/update_staff": "staff", "/add_staff": "staff", "/add_leave": "leaves", "/decide_leave": "leaves",
                    "/bulk_leave": "leaves", "/set_travel_rate": "travel",
                    "/add_travel": "travel", "/add_loan": "loans", "/add_input": "inputs", "/approve_request": "approvals",
                    "/reject_request": "approvals", "/add_user": "users", "/delete_user": "users",
                    "/set_delegate": "users", "/remove_delegate": "users",
                    "/upload_document": "staff", "/save_doc_type": "staff", "/toggle_doc_type": "staff",
                    "/upload_attendance": "attendance", "/map_attendance": "attendance", "/absence_action": "attendance",
                    "/save_timing": "attendance", "/delete_timing": "attendance", "/save_part_hours": "attendance",
                    "/leave_import_upload": "leaves", "/leave_import_apply": "leaves", "/leave_import_discard": "leaves",
                    "/change_password": "account"}.get(parsed.path, "payroll")
        if next_tab not in ROLE_TABS.get(role, ()):
            next_tab = ROLE_TABS.get(role, ("account",))[0]
        if role not in ROLE_ACTIONS.get(parsed.path, ()):
            return self.redirect(f"/?tab={next_tab}&ym={ym}&msg=not_allowed")
        if account["must_change_pw"] and not acting and parsed.path != "/change_password":
            return self.redirect(f"/?tab=account&ym={ym}&msg=pw_must_change")

        conn = get_conn()
        try:
            msg = self.handle_change(conn, parsed.path, data, ym, pm_str, user, role, account["emp_id"])
            conn.commit()
        except (ChangeError, KeyError, ValueError, sqlite3.IntegrityError) as e:
            conn.rollback()
            msg = e.args[0] if isinstance(e, ChangeError) else "bad_input"
        finally:
            conn.close()
        recalc_open_months()
        emp_back = f"&emp={emp_param}" if next_tab in ("timesheets", "staff") and emp_param and role != ROLE_STAFF else ""
        self.redirect(f"/?tab={next_tab}&ym={ym}{emp_back}&msg={msg}" + ("#docs" if parsed.path == "/upload_document" else ""))

    def handle_change(self, conn, path, data, ym, pm_str, user, role, own_emp):
        """Carry out one POSTed action (role already checked against ROLE_ACTIONS); returns a FLASH_MESSAGES key or raises ChangeError."""
        cfg = get_settings_dict(conn)
        cutoff = int(float(cfg.get("cutoff_day", 25)))
        km_rate = float(cfg.get("travel_rate_per_km", 17))
        now = datetime.now().isoformat(timespec="seconds")
        enters_for_others = role in (ROLE_APPROVER, ROLE_PREPARER)

        if path == "/add_staff":
            # every mandatory document for the person's staff type must come with the request (EOBI-only: none)
            docs = []
            for t in doc_types_for(conn, data.get("staff_type")):
                upload = self.files.get(f"doc_{t['code']}")
                upload = upload if upload and upload[2] else None
                expiry = (data.get("cnic_expiry") if t["code"] == "cnic" else data.get(f"expiry_{t['code']}", "")) or ""
                if t["mandatory"] and (not upload or (t["has_expiry"] and not parse_date(expiry))):
                    raise ChangeError("doc_missing")
                if upload:
                    check_document(upload)
                    docs.append((t["code"], upload, fmt_date(parse_date(expiry)) if expiry else None))
            result = submit_change(conn, "add_staff", data, pm_str, cfg, user, role)
            request_id = conn.execute("SELECT MAX(id) FROM change_requests").fetchone()[0]
            emp_id = data.get("emp_id", "").strip().upper()
            for code, upload, expiry in docs:
                save_document(conn, emp_id, code, upload, expiry, user, request_id)
            return result

        if path in ("/update_staff", "/add_input", "/add_loan"):
            return submit_change(conn, path[1:], data, pm_str, cfg, user, role)

        if path == "/settle_security":
            emp_id, decision = data.get("emp_id", "").strip().upper(), data.get("decision")
            note = data.get("note", "").strip()[:300]
            s = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
            left_on = parse_date(s["leaving_date"]) if s else None
            if (s is None or decision not in ("Released", "Forfeited") or is_month_locked(conn, pm_str)
                    or not (s["status"] == "Left" or left_on) or (left_on and fmt_date(first_of_month(left_on)) > pm_str)
                    or conn.execute("SELECT 1 FROM security_settlements WHERE emp_id = ?", (emp_id,)).fetchone()):
                raise ChangeError("security_invalid")
            amount = held_balance(conn, s, pm_str)
            if amount <= 0:
                raise ChangeError("security_invalid")
            if decision == "Released" and data.get("handover") != "1":
                raise ChangeError("security_need_handover")
            if decision == "Forfeited" and not note:
                raise ChangeError("security_need_note")
            conn.execute("""INSERT INTO security_settlements (emp_id, decision, amount, payroll_month, note, decided_by, decided_at)
                            VALUES (?,?,?,?,?,?,?)""", (emp_id, decision, amount, pm_str, note, user, now))
            log_decision(conn, "security", emp_id, pm_str, {"decision": decision, "amount": amount},
                         f"{emp_id} {s['employee_name']}: held pay / security Rs. {amount:,.0f} {decision.lower()} in {pay_month_label(pm_str)}"
                         + (" (notice and handing / taking over confirmed)" if decision == "Released" else f". Reason: {note}"), user)
            return "security_released" if decision == "Released" else "security_forfeited"

        if path == "/leave_import_upload":
            upload = self.files.get("leave_file")
            if not upload or not upload[2] or not upload[2].startswith(b"PK\x03\x04") or len(upload[2]) > MAX_UPLOAD_BYTES:
                raise ChangeError("leave_file_unreadable")
            try:
                parsed = read_leave_history(conn, upload[2])
            except ChangeError:
                raise
            except Exception:
                raise ChangeError("leave_file_unreadable")
            conn.execute("UPDATE leave_imports SET status = 'Discarded' WHERE status = 'Preview'")
            conn.execute("INSERT INTO leave_imports (file_name, uploaded_by, uploaded_at, data) VALUES (?,?,?,?)",
                         (os.path.basename(upload[0].replace("\\", "/"))[:120], user, now, json.dumps(parsed)))
            return "leave_file_previewed"

        if path in ("/leave_import_apply", "/leave_import_discard"):
            imp = conn.execute("SELECT * FROM leave_imports WHERE id = ? AND status = 'Preview'", (int(data["id"]),)).fetchone()
            if imp is None:
                raise ChangeError("bad_input")
            if path == "/leave_import_discard":
                conn.execute("UPDATE leave_imports SET status = 'Discarded' WHERE id = ?", (imp["id"],))
                return "sheet_discarded"
            parsed = json.loads(imp["data"])
            added, skipped = apply_leave_history(conn, parsed, user, data.get("replace") == "1")
            conn.execute("UPDATE leave_imports SET status = 'Applied', applied_at = ? WHERE id = ?", (now, imp["id"]))
            log_decision(conn, "leave_import", None, None, {"import": imp["id"]},
                         f"Leave history '{imp['file_name']}' applied: opening balances (as at 31 Dec 2025) for {len(parsed['opening'])} staff; "
                         f"{added} leave record(s) added as approved, {skipped} already on file"
                         + ("; earlier 2026 records of those staff replaced" if data.get("replace") == "1" else ""), user)
            return "leave_file_applied"

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
            folder = os.path.join(UPLOAD_DIR, "attendance")
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

        if path == "/absence_action":
            emp_id = data.get("emp_id", "").strip().upper()
            action = data.get("action")
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
                             (date.today().isoformat(), emp_id, leave_type, d, d, "Absent per attendance machine report",
                              "Pending" if action == "leave" else "Recorded", pm_target, user))
            return "absences_filed" if action == "leave" else "absences_recorded"

        if path == "/import_payroll_upload":
            upload = self.files.get("sheet")
            if not upload or not upload[2]:
                raise ChangeError("sheet_unreadable")
            name, content = os.path.basename(upload[0].replace("\\", "/"))[:120], upload[2]
            if len(content) > MAX_UPLOAD_BYTES or not (name.lower().endswith((".xlsx", ".xlsm", ".csv")) or content.startswith(b"PK\x03\x04")):
                raise ChangeError("sheet_unreadable")
            try:
                sheet_name, mapping, rows, unmatched = read_payroll_sheet(conn, name, content)
            except ChangeError:
                raise
            except Exception:
                raise ChangeError("sheet_unreadable")
            folder = os.path.join(UPLOAD_DIR, "payroll_sheets")
            os.makedirs(folder, exist_ok=True)
            stored = f"{pm_str[:7]}_{secrets.token_hex(6)}_{''.join(c for c in name if c.isalnum() or c in '._-')}"
            with open(os.path.join(folder, stored), "wb") as f:
                f.write(content)
            conn.execute("UPDATE payroll_imports SET status = 'Discarded' WHERE payroll_month = ? AND status = 'Preview'", (pm_str,))
            conn.execute("""INSERT INTO payroll_imports (payroll_month, file_name, stored_file, sheet_name, uploaded_by, uploaded_at, mapping, rows, unmatched)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (pm_str, name, stored, sheet_name, user, now, json.dumps(mapping), json.dumps(rows), json.dumps(unmatched)))
            return "sheet_previewed"

        if path in ("/import_payroll_apply", "/import_payroll_discard"):
            imp = conn.execute("SELECT * FROM payroll_imports WHERE id = ? AND status = 'Preview'", (int(data["id"]),)).fetchone()
            if imp is None:
                raise ChangeError("bad_input")
            if path == "/import_payroll_discard":
                conn.execute("UPDATE payroll_imports SET status = 'Discarded' WHERE id = ?", (imp["id"],))
                return "sheet_discarded"
            conn.commit()  # apply_payroll_sheet may recalculate the month on its own connection first
            apply_payroll_sheet(conn, imp, user, update_salaries=data.get("update_salaries") == "1")
            total = conn.execute("SELECT SUM(net_pay) FROM payroll_lines WHERE payroll_month = ?", (imp["payroll_month"],)).fetchone()[0] or 0
            log_decision(conn, "payroll_import", None, imp["payroll_month"], {"import": imp["id"], "file": imp["file_name"]},
                         f"{pay_month_label(imp['payroll_month'])} payroll replaced by the final sheet '{imp['file_name']}' "
                         f"({len(json.loads(imp['rows']))} staff, net pay Rs. {total:,.0f}); month approved and locked"
                         + (" — staff salaries updated from the sheet" if data.get("update_salaries") == "1" else ""), user)
            return "sheet_applied"

        if path == "/upload_document":
            emp_id = data.get("emp_id", "").strip().upper()
            staff = conn.execute("SELECT employee_name FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
            dtype = conn.execute("SELECT * FROM doc_types WHERE code = ? AND active = 1", (data.get("doc_type", ""),)).fetchone()
            upload = self.files.get("document")
            expiry = parse_date(data.get("expiry_date"))
            if staff is None or dtype is None or not upload or not upload[2] or (dtype["has_expiry"] and not expiry):
                raise ChangeError("doc_missing")
            save_document(conn, emp_id, dtype["code"], upload, fmt_date(expiry) if expiry else None, user)
            log_decision(conn, "staff_document", emp_id, None, {"doc_type": dtype["code"]},
                         f"{emp_id} {staff['employee_name']}: {dtype['name']} uploaded" + (f" (expires {fmt_date(expiry)})" if expiry else ""), user)
            return "doc_saved"

        if path in ("/save_doc_type", "/toggle_doc_type"):
            if path == "/toggle_doc_type":
                t = conn.execute("SELECT * FROM doc_types WHERE code = ?", (data.get("code", ""),)).fetchone()
                field = data.get("field")
                if t is None or field not in ("mandatory", "active"):
                    raise ChangeError("bad_input")
                conn.execute(f"UPDATE doc_types SET {field} = ? WHERE code = ?", (0 if t[field] else 1, t["code"]))
                summary = f"{t['name']}: {field} {'off' if t[field] else 'on'}"
            else:
                name = data.get("name", "").strip()[:80]
                applies = data.get("applies_to", "All")
                if not name or applies not in ("All", "Permanent", "ICAP Article", NOC_TYPE, "Intern"):
                    raise ChangeError("doc_type_invalid")
                code = "".join(c if c.isalnum() else "_" for c in name.lower())[:30].strip("_")
                if conn.execute("SELECT 1 FROM doc_types WHERE code = ?", (code,)).fetchone():
                    raise ChangeError("doc_type_exists")
                conn.execute("INSERT INTO doc_types (code, name, applies_to, mandatory, has_expiry, added_by, added_at) VALUES (?,?,?,?,?,?,?)",
                             (code, name, applies, 1 if data.get("mandatory") == "1" else 0, 1 if data.get("has_expiry") == "1" else 0, user, now))
                summary = f"New document type '{name}' for {applies}{' (mandatory)' if data.get('mandatory') == '1' else ''}"
            log_decision(conn, "doc_type", None, None, {"path": path}, summary, user)
            return "doc_type_saved"

        if path in ("/approve_request", "/reject_request"):
            return decide_change(conn, int(data["id"]), path == "/approve_request", user)

        if path in ("/prepare_month", "/review_month", "/approve_month", "/send_back_month"):
            conn.execute("INSERT OR IGNORE INTO payroll_runs (payroll_month, status) VALUES (?, 'Draft')", (pm_str,))
            stage = conn.execute("SELECT status FROM payroll_runs WHERE payroll_month = ?", (pm_str,)).fetchone()["status"]
            if path == "/prepare_month":
                if stage != "Draft":
                    raise ChangeError("wrong_stage")
                if not conn.execute("SELECT 1 FROM attendance_uploads WHERE payroll_month = ?", (pm_str,)).fetchone():
                    raise ChangeError("need_attendance")  # the payroll starts with the preparer uploading the attendance
                conn.commit()
                calculate_payroll(pm_str)  # sign off the up-to-date figures
                conn.execute("UPDATE payroll_runs SET status = 'Prepared', prepared_by = ?, prepared_date = ? WHERE payroll_month = ?", (user, now, pm_str))
                return "month_prepared"
            if path == "/review_month":
                if stage != "Prepared":
                    raise ChangeError("wrong_stage")
                conn.execute("UPDATE payroll_runs SET status = 'Reviewed', reviewed_by = ?, reviewed_date = ? WHERE payroll_month = ?", (user, now, pm_str))
                return "month_reviewed"
            if path == "/approve_month":
                if stage != "Reviewed":
                    raise ChangeError("wrong_stage")
                conn.execute("UPDATE payroll_runs SET status = 'Approved', approved_by = ?, approved_date = ? WHERE payroll_month = ?", (user, now, pm_str))
                return "month_approved"
            if stage == "Draft" or (stage == "Approved" and role != ROLE_APPROVER):
                raise ChangeError("wrong_stage" if stage == "Draft" else "not_allowed")
            if conn.execute("SELECT imported_from FROM payroll_runs WHERE payroll_month = ?", (pm_str,)).fetchone()["imported_from"]:
                raise ChangeError("imported_month")  # recalculating would overwrite the final sheet's figures
            conn.execute("""UPDATE payroll_runs SET status = 'Draft', prepared_by = NULL, prepared_date = NULL, reviewed_by = NULL,
                            reviewed_date = NULL, approved_by = NULL, approved_date = NULL WHERE payroll_month = ?""", (pm_str,))
            return "month_sent_back"

        if path == "/save_timesheet":
            emp_id = data.get("emp_id", "").strip().upper() if enters_for_others else own_emp
            if not emp_id:
                raise ChangeError("no_emp_link")
            return save_timesheet(conn, emp_id, pm_str, data, self.files, data.get("action") == "submit", user, cfg)

        if path == "/apply_loan":
            if not own_emp:
                raise ChangeError("no_emp_link")
            staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (own_emp,)).fetchone()
            if staff is None or staff["status"] != "Active" or staff["staff_type"] == EOBI_ONLY_TYPE:
                raise ChangeError("not_employee")
            loan_type = "Advance" if data.get("loan_type") == "Advance" else "Loan"
            amount = round(float(data.get("amount") or 0), 0)
            instalments = 1 if loan_type == "Advance" else int(float(data.get("instalments") or 0))
            purpose = data.get("purpose", "").strip()[:200]
            if amount <= 0 or not purpose or instalments < 1:
                raise ChangeError("loan_invalid")
            if instalments > int(float(cfg.get("loan_max_instalments", 20))):
                raise ChangeError("loan_instalments")
            limit, outstanding, exceeds = loan_policy(conn, staff, amount, pm_str, cfg)
            conn.execute("""INSERT INTO loan_applications (emp_id, loan_type, amount, instalments, purpose, applied_by, applied_at,
                                                           policy_limit, outstanding, exceeds_policy) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (own_emp, loan_type, amount, instalments, purpose, user, now, limit, outstanding, 1 if exceeds else 0))
            return "loan_applied"

        if path in ("/set_delegate", "/remove_delegate"):
            emp_id = data.get("emp_id", "").strip().upper()
            staff = conn.execute("""SELECT s.employee_name FROM staff s JOIN users u ON u.emp_id = s.emp_id
                                    WHERE s.emp_id = ? AND u.role = ?""", (emp_id, ROLE_STAFF)).fetchone()
            if staff is None:
                raise ChangeError("delegate_invalid")
            if path == "/remove_delegate":
                conn.execute("DELETE FROM portal_delegates WHERE emp_id = ?", (emp_id,))
                conn.execute("UPDATE sessions SET acting_for = NULL WHERE acting_for = ?", (emp_id,))
                log_decision(conn, "portal_delegate", emp_id, None, {"delegate": None}, f"{emp_id} {staff['employee_name']}: portal operator removed", user)
                return "delegate_removed"
            delegate = data.get("delegate_username", "")
            d = conn.execute("SELECT username, role, emp_id FROM users WHERE username = ?", (delegate,)).fetchone()
            if d is None or d["role"] == ROLE_STAFF or d["emp_id"] == emp_id:
                raise ChangeError("delegate_invalid")
            conn.execute("INSERT OR REPLACE INTO portal_delegates (emp_id, delegate_username, granted_by, granted_at) VALUES (?,?,?,?)",
                         (emp_id, delegate, user, now))
            log_decision(conn, "portal_delegate", emp_id, None, {"delegate": delegate},
                         f"{emp_id} {staff['employee_name']}: portal operated by {delegate} ({d['role']})", user)
            return "delegate_saved"

        if path == "/export_loans":
            batch, ibft_path, ift_path, paid, skipped = generate_loan_bank_files(conn, user)
            if not paid:
                raise ChangeError("loan_files_none")
            result = {"batch": batch, "by": user, "at": now, "ibft": ibft_path, "ift": ift_path,
                      "paid": [f"{r['loan_ref']} {r['emp_id']} {r['employee_name']}: Rs. {r['amount']:,.0f}" for r in paid],
                      "skipped": [f"{r['loan_ref']} {r['emp_id']} {r['employee_name']}" for r in skipped]}
            conn.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('last_loan_batch', ?, 'Last loan bank-file batch')",
                         (json.dumps(result),))
            log_decision(conn, "add_loan", None, None, {"batch": batch, "loans": [r["loan_ref"] for r in paid]},
                         f"Loan payment batch {batch}: {len(paid)} loan(s), Rs. {sum(r['amount'] for r in paid):,.0f} by separate IBFT / IFT", user)
            return "loan_files_partly" if skipped else "loan_files_done"

        if path in ("/review_loan", "/decide_loan", "/withdraw_loan"):
            la = conn.execute("SELECT a.*, s.employee_name FROM loan_applications a JOIN staff s ON s.emp_id = a.emp_id WHERE a.id = ?",
                              (int(data["id"]),)).fetchone()
            if la is None:
                raise ChangeError("bad_input")
            decision = data.get("decision")
            note = data.get("note", "").strip()[:300]
            if path == "/withdraw_loan":
                if la["emp_id"] != own_emp or la["status"] != "Applied":
                    raise ChangeError("loan_wrong_stage")
                conn.execute("UPDATE loan_applications SET status = 'Withdrawn' WHERE id = ?", (la["id"],))
                return "loan_withdrawn"
            approver_only = la["emp_id"] in approver_only_emps(conn)
            if (own_emp and la["emp_id"] == own_emp) or (role == ROLE_REVIEWER and approver_only):
                raise ChangeError("loan_own")
            desc = (f"{la['emp_id']} {la['employee_name']}: {la['loan_type']} Rs. {la['amount']:,.0f}"
                    + (f" over {la['instalments']} instalments" if la["loan_type"] == "Loan" else "") + f" — {la['purpose']}")
            if path == "/review_loan":
                if la["status"] != "Applied" or decision not in ("Recommended", "Rejected"):
                    raise ChangeError("loan_wrong_stage")
                if decision == "Recommended" and la["exceeds_policy"] and not (data.get("exception") == "1" and note):
                    raise ChangeError("loan_need_exception")
                if decision == "Rejected" and not note:
                    raise ChangeError("claim_need_note")
                conn.execute("UPDATE loan_applications SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ? WHERE id = ?",
                             (decision, user, now, note, la["id"]))
                if decision == "Recommended":
                    notify_approver(conn, user, f"{FIRM_SHORT} payroll — loan application awaiting your approval",
                                    f"{user} recommended a loan application:\n\n  {desc}\n"
                                    + (f"  EXCEEDS POLICY (limit Rs. {la['policy_limit']:,.0f}) — exception: {note}\n" if la["exceeds_policy"] else "")
                                    + f"\nPlease approve or reject it on the Approvals tab: {cfg.get('app_url', '')}\n")
                    return "loan_recommended"
                return "loan_rejected"
            # Approver: a recommended application, or one the reviewer may not review (his own / a portal he operates)
            if not (la["status"] == "Recommended" or (la["status"] == "Applied" and approver_only)) or decision not in ("Approved", "Rejected"):
                raise ChangeError("loan_wrong_stage")
            if decision == "Rejected":
                conn.execute("UPDATE loan_applications SET status = 'Rejected', decided_by = ?, decided_at = ?, decision_note = ? WHERE id = ?",
                             (user, now, note, la["id"]))
                log_decision(conn, "add_loan", la["emp_id"], None, {"application": la["id"]}, f"Application #{la['id']} rejected — {desc}", user)
                return "loan_rejected"
            amount = round(float(data.get("approved_amount") or la["amount"]), 0)
            if not 0 < amount <= la["amount"]:
                raise ChangeError("loan_bad_amount")
            ref, instalment = approve_loan_application(conn, la, amount, user, now)
            conn.execute("""UPDATE loan_applications SET status = 'Approved', approved_amount = ?, decided_by = ?, decided_at = ?, decision_note = ?,
                            loan_ref = ? WHERE id = ?""", (amount, user, now, note, ref, la["id"]))
            log_decision(conn, "add_loan", la["emp_id"], None, {"application": la["id"], "loan_ref": ref, "amount": amount},
                         f"Application #{la['id']} approved as {ref}: Rs. {amount:,.0f}"
                         + (f", Rs. {instalment:,.0f}/month" if la["loan_type"] == "Loan" else ", recovered in full next month")
                         + (" (EXCEPTION to loan policy)" if la["exceeds_policy"] else "") + f" — {desc}", user)
            return "loan_approved"

        if path in ("/add_holiday", "/delete_holiday"):
            d = parse_date(data.get("holiday_date"))
            name = data.get("name", "").strip()[:80]
            if d is None or (path == "/add_holiday" and not name):
                raise ChangeError("holiday_invalid")
            if path == "/add_holiday":
                conn.execute("INSERT OR REPLACE INTO holidays (holiday_date, name, added_by, added_at) VALUES (?,?,?,?)", (fmt_date(d), name, user, now))
                summary = f"{d.strftime('%a %d %b %Y')}: {name}"
            else:
                conn.execute("DELETE FROM holidays WHERE holiday_date = ?", (fmt_date(d),))
                summary = f"{d.strftime('%a %d %b %Y')} removed"
            log_decision(conn, "holiday", None, None, {"date": fmt_date(d), "name": name}, summary, user)
            if d < date.today():  # a past date entered for the record: nothing to announce
                return "holiday_saved" if path == "/add_holiday" else "holiday_deleted"
            conn.commit()  # the holiday stands whatever happens to the email
            when = d.strftime("%a %d %b %Y")
            headline = f"A gazetted holiday has been announced: {when}, {name}." if path == "/add_holiday" else f"The gazetted holiday on {when} has been cancelled."
            return {"sent": "holiday_notice_sent", "partly": "holiday_notice_partly", "failed": "holiday_notice_failed",
                    "no_account": "holiday_notice_no_account", "no_staff": "holiday_notice_no_staff"}[email_holiday_notice(conn, user, now, headline)]

        if path == "/email_holidays":
            outcome = email_holiday_notice(conn, user, now)
            if outcome == "no_account":
                raise ChangeError("mail_no_account")
            if outcome in ("no_staff", "nothing"):
                raise ChangeError("holidays_none")
            return {"sent": "holidays_emailed", "failed": "mail_failed", "partly": "holidays_email_partly"}[outcome]

        if path == "/apply_credit":
            if not own_emp:
                raise ChangeError("no_emp_link")
            staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (own_emp,)).fetchone()
            if staff is None or staff["status"] != "Active" or staff["staff_type"] == EOBI_ONLY_TYPE:
                raise ChangeError("not_employee")
            if data.get("full_day") != "1":
                raise ChangeError("credit_full_day")
            note = data.get("note", "").strip()[:200]
            if not note:
                raise ChangeError("credit_need_note")
            d = parse_date(data.get("work_date"))
            if d is None:
                raise ChangeError("credit_date")
            holiday = conn.execute("SELECT 1 FROM holidays WHERE holiday_date = ?", (fmt_date(d),)).fetchone()
            reason = "weekend_called" if not holiday else "holiday_client" if staff["staff_type"] in ARTICLE_TYPES else "holiday_called"
            work_date = check_credit(conn, staff, data.get("work_date"), reason)
            late = credit_is_late(work_date, cfg)
            conn.execute("""INSERT INTO leave_credits (emp_id, work_date, days, reason, note, source, status, requested_by, requested_at, applied_late)
                            VALUES (?,?,1,?,?,'Application',?,?,?,?)""", (own_emp, work_date, reason, note, "Late" if late else "Applied", user, now, 1 if late else 0))
            if own_emp in approver_only_emps(conn):
                notify_approver(conn, user, f"{FIRM_SHORT} payroll — credit leave application awaiting your decision",
                                f"{own_emp} {staff['employee_name']} applied for one credit leave for {work_date}: {note}\n\n"
                                f"Please decide it on the Approvals tab: {cfg.get('app_url', '')}\n")
            return "credit_submitted_late" if late else "credit_submitted"

        if path == "/review_credit":
            credit = conn.execute("SELECT c.*, s.employee_name FROM leave_credits c JOIN staff s ON s.emp_id = c.emp_id WHERE c.id = ?",
                                  (int(data["id"]),)).fetchone()
            if credit is None or credit["status"] not in ("Applied", "Late"):
                raise ChangeError("credit_wrong_stage")
            decision, note = data.get("decision"), data.get("note", "").strip()[:200]
            only_approver = credit["emp_id"] in approver_only_emps(conn)
            if role == ROLE_REVIEWER and only_approver:
                raise ChangeError("credit_own")
            if role == ROLE_APPROVER and not only_approver:
                raise ChangeError("credit_wrong_stage")  # the reviewer recommends or condones it first
            if decision not in ("Recommended", "Rejected"):
                raise ChangeError("bad_input")
            late = credit["status"] == "Late"
            if (late or decision == "Rejected") and not note:
                raise ChangeError("credit_need_note")
            desc = f"{credit['emp_id']} {credit['employee_name']}: credit leave for {credit['work_date']}"
            if decision == "Rejected":
                conn.execute("UPDATE leave_credits SET status = 'Rejected', decided_by = ?, decided_at = ?, condone_note = ? WHERE id = ?", (user, now, note, credit["id"]))
                log_decision(conn, "leave_credit", credit["emp_id"], None, {"id": credit["id"], "decision": "Rejected"}, f"{desc} rejected — {note}", user)
                return "credit_rejected"
            if late:
                conn.execute("UPDATE leave_credits SET condoned_by = ?, condoned_at = ?, condone_note = ? WHERE id = ?", (user, now, note, credit["id"]))
            if role == ROLE_APPROVER:
                conn.execute("UPDATE leave_credits SET status = 'Approved', decided_by = ?, decided_at = ? WHERE id = ?", (user, now, credit["id"]))
                log_decision(conn, "leave_credit", credit["emp_id"], None, {"id": credit["id"], "decision": "Approved"},
                             f"{desc} approved" + (f"; delay condoned — {note}" if late else ""), user)
                return "credit_approved"
            conn.execute("UPDATE leave_credits SET status = 'Recommended', recommended_by = ?, recommended_at = ? WHERE id = ?", (user, now, credit["id"]))
            log_decision(conn, "leave_credit", credit["emp_id"], None, {"id": credit["id"], "decision": "Recommended", "condoned": late},
                         f"{desc} recommended" + (f"; delay condoned — {note}" if late else ""), user)
            notify_approver(conn, user, f"{FIRM_SHORT} payroll — leave credit awaiting your approval",
                            f"{user} recommended a credit leave:\n\n  {desc}\n" + (f"  Delay condoned: {note}\n" if late else "") +
                            f"\nPlease approve or reject it on the Approvals tab: {cfg.get('app_url', '')}\n")
            return "credit_condoned" if late else "credit_recommended"

        if path == "/add_credit":
            emp_id = data.get("emp_id", "").strip().upper()
            staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
            if staff is None or staff["status"] != "Active" or staff["staff_type"] == EOBI_ONLY_TYPE:
                raise ChangeError("emp_missing")
            if role == ROLE_REVIEWER and emp_id == own_emp:
                raise ChangeError("credit_own")
            reason = data.get("reason", "")
            work_date = check_credit(conn, staff, data.get("work_date"), reason)
            days = 1.0  # credit leave is earned only for a complete working day
            approved = role == ROLE_APPROVER
            note = data.get("note", "").strip()[:200]
            late = credit_is_late(work_date, cfg)
            if late and not approved and not note:
                raise ChangeError("credit_need_note")  # entered after the limit: the reviewer records why the delay is condoned
            conn.execute("""INSERT INTO leave_credits (emp_id, work_date, days, reason, note, source, status, requested_by, requested_at, decided_by, decided_at,
                                                       applied_late, condoned_by, condoned_at, condone_note)
                            VALUES (?,?,?,?,?,'Reviewer',?,?,?,?,?,?,?,?,?)""",
                         (emp_id, work_date, days, reason, note, "Approved" if approved else "Recommended", user, now,
                          user if approved else None, now if approved else None,
                          1 if late else 0, user if late else None, now if late else None, note if late else None))
            summary = f"{emp_id} {staff['employee_name']}: +{days:g} leave for {parse_date(work_date).strftime('%a %d %b %Y')} — {CREDIT_REASONS[reason]}"
            if approved:
                log_decision(conn, "leave_credit", emp_id, None, {"work_date": work_date, "days": days, "reason": reason}, summary, user)
                return "credit_applied"
            notify_approver(conn, user, f"{FIRM_SHORT} payroll — leave credit awaiting your approval",
                            f"{user} recommended a leave credit:\n\n  {summary}\n{('  Note: ' + note) if note else ''}\n\n"
                            f"Please approve or reject it on the Approvals tab: {cfg.get('app_url', '')}\n")
            return "credit_added"

        if path == "/decide_credit":
            credit = conn.execute("SELECT c.*, s.employee_name FROM leave_credits c JOIN staff s ON s.emp_id = c.emp_id WHERE c.id = ?",
                                  (int(data["id"]),)).fetchone()
            if credit is None or credit["status"] != "Recommended":
                raise ChangeError("credit_wrong_stage")
            decision = "Approved" if data.get("decision") == "Approved" else "Rejected"
            conn.execute("UPDATE leave_credits SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?", (decision, user, now, credit["id"]))
            log_decision(conn, "leave_credit", credit["emp_id"], None, {"id": credit["id"], "decision": decision},
                         f"{credit['emp_id']} {credit['employee_name']}: +{credit['days']:g} leave for {credit['work_date']} "
                         f"({CREDIT_REASONS.get(credit['reason'], credit['reason'])}) — {decision.lower()}", user)
            return "credit_approved" if decision == "Approved" else "credit_rejected"

        if path == "/set_approver_email":
            addr = data.get("approver_email", "").strip().lower()
            if addr and not valid_email(addr):
                raise ChangeError("email_invalid")
            conn.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('approver_notify_email', ?, 'Approver is emailed here when something awaits approval')",
                         (addr,))
            return "approver_email_saved"

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
                (to, err), = send_emails(account, [(account["from_address"], f"{FIRM_SHORT} Payroll — test email",
                                                    f"This is a test email from the {FIRM_NAME} payroll system, sent by {user}.\n")])
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
            app_url = cfg.get("app_url", f"http://127.0.0.1:{PORT}")
            messages, passwords = [], {}
            for t in targets:
                passwords[t["username"]] = starting_password(t["employee_name"])
                messages.append((t["email"], f"Your login for the {FIRM_SHORT} payroll system",
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

        if path == "/set_ts_access":
            emp_id = data.get("emp_id", "").strip().upper()
            staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
            if (staff is None or staff["status"] != "Active" or staff["staff_type"] in ARTICLE_TYPES + (EOBI_ONLY_TYPE,)):
                raise ChangeError("ts_access_invalid")
            if data.get("enable") == "1":
                start = parse_date(f"{data.get('from_month', '')}-01")
                if not start:
                    raise ChangeError("ts_access_invalid")
                conn.execute("UPDATE staff SET timesheet_from = ? WHERE emp_id = ?", (fmt_date(start), emp_id))
                summary = f"{emp_id} {staff['employee_name']}: timesheets required from {pay_month_label(fmt_date(start))} (salary held until approved)"
                result = "ts_access_on"
            else:
                conn.execute("UPDATE staff SET timesheet_from = NULL WHERE emp_id = ?", (emp_id,))
                summary = f"{emp_id} {staff['employee_name']}: timesheets no longer required"
                result = "ts_access_off"
            log_decision(conn, "ts_access", emp_id, None, {"timesheet_from": data.get("from_month") if result == "ts_access_on" else None}, summary, user)
            return result

        if path == "/save_ts_code":
            category, code = data.get("category"), data.get("code", "").strip().upper()
            description = data.get("description", "").strip()[:120]
            if category not in TS_CODE_FIELDS or not code or len(code) > 12 or not all(c.isalnum() or c in "-/" for c in code):
                raise ChangeError("code_invalid")
            old = conn.execute("SELECT description FROM timesheet_codes WHERE category = ? AND code = ?", (category, code)).fetchone()
            conn.execute("""INSERT INTO timesheet_codes (category, code, description, added_by, added_at) VALUES (?,?,?,?,?)
                            ON CONFLICT(category, code) DO UPDATE SET description = excluded.description""",
                         (category, code, description, user, now))
            change = (f"description '{old['description'] or ''}' → '{description}'" if old else f"added{': ' + description if description else ''}")
            log_decision(conn, "ts_code", None, None, {"category": category, "code": code, "description": description},
                         f"{TS_TEXT_FIELDS[category]} {code} {change}", user)
            return "code_saved"

        if path == "/decide_timesheet":
            ts = get_timesheet(conn, data.get("emp_id", ""), pm_str)
            if ts is None:
                raise ChangeError("bad_input")
            return decide_timesheet(conn, ts, data.get("decision"), data.get("note", ""), user, role, own_emp)

        if path == "/decide_leave":
            leave = conn.execute("SELECT * FROM leave_register WHERE id = ?", (int(data["id"]),)).fetchone()
            if leave is None:
                raise ChangeError("bad_input")
            decide_leave(conn, leave, data.get("decision"), user, role, own_emp)
            return {"Recommended": "leave_recommended", "Approved": "leave_approved",
                    "Rejected": "leave_rejected", "Cancelled": "leave_cancelled"}[data["decision"]]

        if path == "/bulk_leave":
            # Reviewer: recommend every Pending leave; Approver: approve every Recommended one. Skips what this user may not decide.
            decision = data.get("decision")
            if (decision, role) not in (("Recommended", ROLE_REVIEWER), ("Approved", ROLE_APPROVER)):
                raise ChangeError("not_allowed")
            for leave in conn.execute("SELECT * FROM leave_register WHERE status IN ('Pending', 'Recommended') ORDER BY id").fetchall():
                try:
                    decide_leave(conn, leave, decision, user, role, own_emp)
                except ChangeError:
                    continue
            return "leaves_bulk"

        if path == "/set_travel_rate":
            # the formula: Rs. <rate> per km when petrol (Premier Euro 5) costs Rs. <base_price> a litre
            rate = float(data.get("rate") or 0)
            base_price = float(data.get("base_price") or cfg.get("travel_rate_base_price", 400))
            if rate <= 0 or base_price <= 0:
                raise ChangeError("rate_invalid")
            old = f"Rs. {float(cfg.get('travel_rate_base_km', 17)):g}/km at Rs. {float(cfg.get('travel_rate_base_price', 400)):g}/litre"
            for key, value in (("travel_rate_base_km", rate), ("travel_rate_base_price", base_price), ("travel_rate_per_km", rate)):
                conn.execute("UPDATE settings SET value = ? WHERE key = ?", (f"{value:g}", key))
            log_decision(conn, "travel_rate", None, None, {"base_km": rate, "base_price": base_price},
                         f"Travel rate formula {old} → Rs. {rate:g}/km at Rs. {base_price:g}/litre (claims entered from now on)", user)
            return "rate_updated"

        if path == "/set_rate_uplift":
            # Reviewer: raise the formula rate by at most 10% (0 = back to the formula), from today, with a written reason.
            # Approver: any uplift, from any date.
            pct = round(float(data.get("pct") or 0), 2)
            reason = data.get("reason", "").strip()[:300]
            start = date.today() if role == ROLE_REVIEWER else (parse_date(data.get("effective_from")) or date.today())
            limit = float(cfg.get("travel_rate_max_uplift_pct", 10)) if role == ROLE_REVIEWER else 100.0
            if not 0 <= pct <= limit:
                raise ChangeError("uplift_too_high")
            if len(reason) < 10:
                raise ChangeError("uplift_need_reason")
            conn.execute("INSERT INTO rate_uplifts (effective_from, pct, reason, set_by, set_at) VALUES (?,?,?,?,?)",
                         (fmt_date(start), pct, reason, user, now))
            formula_rate = km_rate_from_price(petrol_price_on(conn, start)[1], cfg)
            summary = (f"Travel rate uplift {pct:g}% from {start.strftime('%d %b %Y')}: formula Rs. {formula_rate:g}/km → "
                       f"Rs. {travel_rate_on(conn, start, cfg):g}/km. Reason: {reason}")
            log_decision(conn, "travel_rate", None, None, {"pct": pct, "from": fmt_date(start), "reason": reason}, summary, user)
            if role == ROLE_REVIEWER:
                notify_approver(conn, user, f"{FIRM_SHORT} payroll — travel rate increased by {pct:g}%",
                                f"{user} set a travel rate uplift:\n\n  {summary}\n\nYou can change it on the Travel Claims tab: {cfg.get('app_url', '')}\n")
            return "uplift_saved"

        if path == "/fetch_fuel_prices":
            try:
                added = fetch_fuel_prices(conn, pages=2)
            except Exception as e:
                conn.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('fuel_price_last_check', ?, 'Last PSO price check')",
                             (f"{now[:16]} Failed — {type(e).__name__}: {str(e)[:150]}",))
                conn.commit()
                raise ChangeError("fuel_fetch_failed")
            conn.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('fuel_price_last_check', ?, 'Last PSO price check')",
                         (f"{now[:16]} OK — {added} new price(s)",))
            return "fuel_fetched"

        if path == "/add_fuel_price":
            d, price = parse_date(data.get("effective_date")), float(data.get("price") or 0)
            if d is None or price <= 0 or d > date.today():
                raise ChangeError("fuel_price_invalid")
            conn.execute("INSERT OR REPLACE INTO fuel_prices (effective_date, price, source, recorded_by, recorded_at) VALUES (?,?,'Manual',?,?)",
                         (fmt_date(d), price, user, now))
            log_decision(conn, "travel_rate", None, None, {"date": fmt_date(d), "price": price},
                         f"Petrol (Premier Euro 5) price entered by hand: Rs. {price:g}/litre from {fmt_date(d)} → Rs. {travel_rate_on(conn, d, cfg):g}/km", user)
            return "fuel_price_saved"

        if path == "/release_salary":
            emp_id, withheld_month = data["emp_id"], data["withheld_month"]
            line = conn.execute("""SELECT p.salary_withheld_absence, p.employee_name, run.status FROM payroll_lines p
                                   JOIN payroll_runs run ON run.payroll_month = p.payroll_month
                                   WHERE p.emp_id = ? AND p.payroll_month = ?""", (emp_id, withheld_month)).fetchone()
            released = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM salary_releases WHERE emp_id = ? AND withheld_month = ?",
                                    (emp_id, withheld_month)).fetchone()[0]
            if (line is None or line["status"] != "Approved" or withheld_month >= pm_str or is_month_locked(conn, pm_str)
                    or line["salary_withheld_absence"] - released <= 0):
                raise ChangeError("release_invalid")
            amount = line["salary_withheld_absence"] - released
            conn.execute("INSERT INTO salary_releases (emp_id, withheld_month, amount, release_month, released_by, released_at) VALUES (?,?,?,?,?,?)",
                         (emp_id, withheld_month, amount, pm_str, user, now))
            log_decision(conn, "salary_release", emp_id, pm_str, {"withheld_month": withheld_month, "amount": amount},
                         f"{emp_id} {line['employee_name']}: Rs. {amount:,.0f} withheld in {pay_month_label(withheld_month)} "
                         f"released in {pay_month_label(pm_str)}", user)
            return "salary_released"

        if path == "/save_part_hours":
            emp_id = data.get("emp_id", "").strip().upper()
            staff = conn.execute("SELECT * FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()
            if staff is None:
                raise ChangeError("emp_missing")
            if role == ROLE_REVIEWER and emp_id == own_emp:
                raise ChangeError("hours_own")
            if is_month_locked(conn, pm_str):
                raise ChangeError("locked")
            entered = float(data.get("weekly_hours") or 0)
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

        if path == "/change_password":
            if (not verify_login(user, data.get("current", "")) or len(data.get("new", "")) < MIN_PASSWORD_LEN
                    or data.get("new") != data.get("confirm") or data.get("new") == data.get("current")):
                raise ChangeError("pw_invalid")
            set_password(user, data["new"])
            return "pw_changed"

        if path == "/add_user":
            username = data.get("username", "").strip()
            new_role = data.get("role")
            emp_id = data.get("emp_id", "").strip().upper() or None
            if not username or len(data.get("password", "")) < MIN_PASSWORD_LEN or new_role not in ROLES:
                raise ChangeError("user_invalid")
            if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise ChangeError("user_exists")
            if (new_role == ROLE_STAFF and not emp_id) or (emp_id and not conn.execute("SELECT 1 FROM staff WHERE emp_id = ?", (emp_id,)).fetchone()):
                raise ChangeError("need_emp")
            conn.commit()
            create_user(username, data["password"], new_role, emp_id, must_change=True)
            return "user_added"

        if path == "/delete_user":
            if data.get("username") == user:
                raise ChangeError("cannot_delete_self")
            conn.commit()
            delete_user(data.get("username", ""))
            return "user_deleted"

        if path == "/add_leave":
            emp_id = data["emp_id"].strip().upper() if enters_for_others else own_emp
            if not emp_id:
                raise ChangeError("no_emp_link")
            f_d = parse_date(data["date_from"])
            pm_target = fmt_date(get_payroll_month_for_event(f_d, cutoff))
            check_entry(conn, emp_id, pm_target)
            if data["leave_type"] not in LEAVE_TYPES:
                raise ChangeError("bad_input")
            # Every leave starts Pending and goes through the chain; an absence is a record, not an application
            if data["leave_type"] == ABSENCE_LEAVE_TYPE:
                if not enters_for_others:
                    raise ChangeError("no_absence_entry")
                status = "Recorded"
            else:
                status = "Pending"
            conn.execute("""
                INSERT INTO leave_register (application_date, emp_id, leave_type, date_from, date_to, no_of_days, reason, status, payroll_month, entered_by)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (data["application_date"], emp_id, data["leave_type"],
                  data["date_from"], data["date_to"], float(data["no_of_days"]), data.get("reason",""),
                  status, pm_target, user))
            return "saved"

        if path == "/add_travel":
            emp_id = data["emp_id"].strip().upper() if enters_for_others else own_emp
            if not emp_id:
                raise ChangeError("no_emp_link")
            c_d = parse_date(data["claim_date"])
            if c_d is None or c_d > date.today():
                raise ChangeError("claim_future")
            pm_target = fmt_date(get_payroll_month_for_event(c_d, cutoff))
            check_entry(conn, emp_id, pm_target)
            mode = data.get("travel_mode") or OWN_VEHICLE
            if mode not in TRAVEL_MODES:
                raise ChangeError("bad_input")
            kms = float(data.get("kms") or 0)
            park = float(data.get("parking") or 0)
            other = float(data.get("other_charges") or 0)
            fare = float(data.get("fare") or 0)
            if mode == OWN_VEHICLE:  # kms x the petrol-linked rate on the day travelled
                km_rate, fare = travel_rate_on(conn, c_d, cfg), 0.0
                tot = round(kms * km_rate + park + other, 0)
            else:  # ride service: the actual fare, reimbursed once the reviewer approves it
                km_rate = 0.0
                if fare <= 0:
                    raise ChangeError("claim_need_fare")
                tot = round(fare + park + other, 0)
            if tot <= 0 or kms < 0 or park < 0 or other < 0:
                raise ChangeError("bad_input")
            receipt = self.files.get("receipt")
            receipt_cols = (None, None, None)
            if receipt and receipt[2]:
                name, content, mime, ext = check_document(receipt)
                folder = os.path.join(UPLOAD_DIR, "receipts")
                os.makedirs(folder, exist_ok=True)
                stored = f"receipt_{emp_id}_{c_d.isoformat()}_{secrets.token_hex(8)}.{ext}"
                with open(os.path.join(folder, stored), "wb") as f:
                    f.write(content)
                receipt_cols = (stored, name, mime)
            conn.execute("""
                INSERT INTO travel_claims (claim_date, emp_id, client_name, venue_particular, kms, rate_per_km, parking, other_charges, total_amount,
                                           payroll_month, status, submitted_by, travel_mode, fare, receipt_file, receipt_name, receipt_type)
                VALUES (?,?,?,?,?,?,?,?,?,?,'Submitted',?,?,?,?,?,?)
            """, (data["claim_date"], emp_id, data.get("client_name",""),
                  data.get("venue_particular",""), kms, km_rate, park, other, tot, pm_target, user, mode, fare, *receipt_cols))
            return "claim_submitted"

        if path in ("/review_travel", "/withdraw_travel"):
            claim = conn.execute("SELECT * FROM travel_claims WHERE id = ?", (int(data["id"]),)).fetchone()
            if claim is None:
                raise ChangeError("bad_input")
            if path == "/review_travel":
                return review_travel(conn, claim, data.get("decision"), data.get("chargeable") == "1",
                                     data.get("client_name", ""), data.get("note", ""), user, own_emp, role)
            if claim["status"] != "Submitted":
                raise ChangeError("claim_wrong_stage")
            if not enters_for_others and claim["emp_id"] != own_emp:
                raise ChangeError("not_allowed")
            conn.execute("DELETE FROM travel_claims WHERE id = ?", (claim["id"],))
            return "claim_withdrawn"

        if path == "/mark_invoiced":
            client, month = data.get("client_name", ""), data.get("payroll_month", "")
            cur = conn.execute("""
                UPDATE travel_claims SET invoiced = 1, invoice_no = ?, invoiced_by = ?, invoiced_at = ?
                WHERE status = 'Reviewed' AND chargeable = 1 AND invoiced = 0 AND client_name = ? AND payroll_month = ?
                  AND payroll_month IN (SELECT payroll_month FROM payroll_runs WHERE status = 'Approved')
            """, (data.get("invoice_no", "").strip()[:40], user, now, client, month))
            if cur.rowcount == 0:
                raise ChangeError("invoice_invalid")
            return "invoiced"

        raise ChangeError("bad_input")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Payroll web app")
    parser.add_argument("--company", choices=sorted(COMPANIES), default=DEFAULT_COMPANY,
                        help="which company's payroll to open (each has its own database)")
    args = parser.parse_args()
    use_company(args.company)
    init_db()
    seed_from_excel()
    recalc_open_months()  # also marks staff whose leaving date has passed as Left
    fuel_price_updater()  # keeps the PSO petrol price (and so the travel rate) current
    calculate_payroll("2026-11-01")
    print(f"Starting {FIRM_NAME} Payroll Web App on http://127.0.0.1:{PORT} ...")
    print(f"Database: {DB_PATH}")
    HTTPServer(("127.0.0.1", PORT), PayrollHandler).serve_forever()
