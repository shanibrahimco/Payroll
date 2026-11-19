"""Travel claims: modes, review and client invoicing."""

from datetime import datetime

from auth.roles import ROLE_REVIEWER
from auth.sessions import approver_only_emps
from common.dates import add_months, fmt_date, parse_date
from common.errors import ChangeError
from common.settings import is_month_locked

OWN_VEHICLE = "Own vehicle"  # paid kms x the petrol-linked rate

RIDE_SERVICES = ("InDrive", "Yango", "Cab / Careem / Uber", "Auto rickshaw", "Bike ride service")  # paid at the actual fare

TRAVEL_MODES = (OWN_VEHICLE,) + RIDE_SERVICES

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
