"""Staff documents: CNIC, qualifications, CV and so on."""

from datetime import datetime
import os
import secrets

import config
from common.constants import EOBI_ONLY_TYPE, MAX_UPLOAD_BYTES, NOC_TYPE, SCAN_TYPES
from common.errors import ChangeError

def doc_types_for(conn, staff_type, mandatory_only=False):
    """Active document types that apply to this staff type (EOBI-only people are not employees: none)."""
    if staff_type == EOBI_ONLY_TYPE:
        return []
    also = "ICAP Article" if staff_type == NOC_TYPE else staff_type or ""  # NOC students need what ICAP students need
    return conn.execute(f"""SELECT * FROM doc_types WHERE active = 1 AND (applies_to = 'All' OR applies_to = ? OR applies_to = ?)
                            {'AND mandatory = 1' if mandatory_only else ''} ORDER BY mandatory DESC, name""", (staff_type or "", also)).fetchall()

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
    folder = os.path.join(config.UPLOAD_DIR, "documents")
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
    folder = os.path.join(config.UPLOAD_DIR, "documents")
    for d in conn.execute("SELECT file FROM staff_documents WHERE change_request_id = ?", (request_id,)).fetchall():
        try:
            os.remove(os.path.join(folder, d["file"]))
        except OSError:
            pass
    conn.execute("DELETE FROM staff_documents WHERE change_request_id = ?", (request_id,))
