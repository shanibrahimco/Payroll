"""Bank file exports (IBFT and Meezan IFT)."""

from datetime import datetime
import csv
import os

import config
from common.dates import first_of_month, fmt_date, parse_date
from database import get_conn

def generate_bank_files(payroll_month_str, out_dir=None):
    if out_dir is None:
        out_dir = config.EXPORT_DIR
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
          AND COALESCE(TRIM(s.bank_account_iban), '') != '' AND COALESCE(TRIM(b.ibft_bank_code), '') != ''
        ORDER BY p.emp_id
    """, (pm_str,)).fetchall()

    ibft_path = os.path.join(out_dir, f"IBFT_{pm.strftime('%b_%Y')}.csv")
    write_ibft_file(ibft_path, [(r, r["net_pay"], "".join(r["employee_name"].split())[:14] + mon_tag) for r in ibft_rows])

    ift_rows = conn.execute("""
        SELECT p.emp_id, p.employee_name, p.net_pay, s.bank_account_iban, s.account_title
        FROM payroll_lines p
        JOIN staff s ON p.emp_id = s.emp_id
        WHERE p.payroll_month = ? AND p.payment_mode = 'Meezan IFT' AND p.net_pay > 0
          AND COALESCE(TRIM(s.bank_account_iban), '') != ''
        ORDER BY p.emp_id
    """, (pm_str,)).fetchall()

    ift_path = os.path.join(out_dir, f"Meezan_IFT_{pm.strftime('%b_%Y')}.csv")
    write_ift_file(ift_path, [(r, r["net_pay"], f"Salary {pm.strftime('%b-%y')} {r['emp_id']}") for r in ift_rows])

    conn.close()
    return ibft_path, ift_path

def staff_left_out_of_bank_files(payroll_month_str):
    """People due net pay this month who can't go in a bank file because their bank details are incomplete."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT p.emp_id, p.employee_name, p.net_pay FROM payroll_lines p
        JOIN staff s ON p.emp_id = s.emp_id
        LEFT JOIN banks b ON s.bank_name = b.bank_name
        WHERE p.payroll_month = ? AND p.net_pay > 0 AND p.payment_mode IN ('IBFT', 'Meezan IFT')
          AND (COALESCE(TRIM(s.bank_account_iban), '') = ''
               OR (p.payment_mode = 'IBFT' AND COALESCE(TRIM(b.ibft_bank_code), '') = ''))
        ORDER BY p.emp_id
    """, (fmt_date(first_of_month(parse_date(payroll_month_str))),)).fetchall()
    conn.close()
    return rows

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
    out_dir = out_dir or os.path.join(config.EXPORT_DIR, "loans")
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
