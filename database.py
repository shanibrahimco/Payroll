"""Database schema, connection and upgrades."""

import sqlite3

import config
from common.constants import DEFAULT_DOC_TYPES, TS_DEFAULT_CODES

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
    check_in_time TEXT, -- HH:MM, mobile app
    check_out_time TEXT, -- HH:MM, mobile app
    source TEXT DEFAULT 'Biometric', -- where status / arrival came from: Biometric or Mobile
    in_lat REAL,
    in_lng REAL,
    out_lat REAL,
    out_lng REAL,
    location_note TEXT, -- e.g. Office, or Client Site: ABC Pvt Ltd
    location_status TEXT, -- Verified (at an approved location), Review (elsewhere, awaiting a decision), Accepted, Rejected
    distance_m REAL, -- metres from the nearest approved location when checked in elsewhere
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    PRIMARY KEY (emp_id, att_date)
);

CREATE TABLE IF NOT EXISTS work_locations ( -- places where a mobile check-in is accepted without review
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL, -- Office or Client Site
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    radius_m REAL NOT NULL DEFAULT 150,
    active INTEGER NOT NULL DEFAULT 1,
    added_by TEXT,
    added_at TEXT
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
    acting_for TEXT, -- emp_id whose portal this user is currently operating (portal_delegates)
    client_type TEXT DEFAULT 'web' -- web, or mobile (longer expiry, sent as a Bearer token)
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
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def set_public_url(url):
    """Record the address staff open, so login emails link to the live site."""
    conn = get_conn()
    conn.execute("UPDATE settings SET value = ? WHERE key = 'app_url'", (url,))
    conn.commit()
    conn.close()

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
    attendance_cols = {r["name"] for r in conn.execute("PRAGMA table_info(attendance)")}
    for col, col_type in (("check_in_time", "TEXT"), ("check_out_time", "TEXT"), ("source", "TEXT DEFAULT 'Biometric'"), ("in_lat", "REAL"),
                          ("in_lng", "REAL"), ("out_lat", "REAL"), ("out_lng", "REAL"), ("location_note", "TEXT"), ("location_status", "TEXT"),
                          ("distance_m", "REAL"), ("reviewed_by", "TEXT"), ("reviewed_at", "TEXT"), ("review_note", "TEXT")):
        if col not in attendance_cols:
            conn.execute(f"ALTER TABLE attendance ADD COLUMN {col} {col_type}")
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
    if "client_type" not in {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}:
        conn.execute("ALTER TABLE sessions ADD COLUMN client_type TEXT DEFAULT 'web'")
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
