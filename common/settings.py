"""Firm settings, month locks and the approval log."""

from datetime import datetime
import json

def get_settings_dict(conn):
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}

def is_month_locked(conn, payroll_month_str):
    """Monthly sign-off runs Draft -> Prepared -> Reviewed -> Approved. From Prepared onward the month is locked:
    its figures are never recalculated and nothing can be entered for it until it is sent back to Draft."""
    row = conn.execute("SELECT status FROM payroll_runs WHERE payroll_month = ?", (payroll_month_str,)).fetchone()
    return row is not None and row["status"] != "Draft"

def log_decision(conn, kind, emp_id, month, payload, summary, user):
    """Record an action that needs no separate approval (rate change, salary release) in the approval log."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        INSERT INTO change_requests (kind, emp_id, payroll_month, payload, summary, requested_by, requested_at, status, decided_by, decided_at)
        VALUES (?,?,?,?,?,?,?,'Approved',?,?)
    """, (kind, emp_id, month, json.dumps(payload), summary, user, now, user, now))
