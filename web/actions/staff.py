"""Staff records, documents and held pay."""

from common.constants import NOC_TYPE
from common.dates import first_of_month, fmt_date, parse_date, pay_month_label
from common.errors import ChangeError
from common.settings import is_month_locked, log_decision
from payroll.calculator import held_balance
from payroll.changes import decide_change, submit_change
from staff.documents import check_document, doc_types_for, save_document

def handle(self, conn, path, data, ym, pm_str, user, role, own_emp, cfg, cutoff, km_rate, now, enters_for_others):
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
