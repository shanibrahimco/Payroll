"""Company settings: each firm has its own database, folders, users and port."""

import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Settings for a server. None of these are needed on this computer: without them the app works from the project folder.
DATA_DIR = os.path.abspath(os.environ.get("PAYROLL_DATA_DIR") or BASE_DIR)  # databases, uploads and exports, kept apart from the code
DEFAULT_HOST = os.environ.get("PAYROLL_HOST") or "127.0.0.1"
BEHIND_HTTPS_PROXY = os.environ.get("PAYROLL_BEHIND_HTTPS_PROXY") == "1"  # visitors arrive through the HTTPS proxy (Caddy)
PUBLIC_URL = (os.environ.get("PAYROLL_PUBLIC_URL") or "").strip().rstrip("/")  # the address staff open, used in login emails

# Each company has its own database, exports folder, users and port, so one firm never sees another's payroll.
# Choose with:  python app.py --company elite
COMPANIES = {
    "sic": {
        "name": "Shan Ibrahim & Co. Chartered Accountants",
        "short": "Shan Ibrahim & Co.",
        "db": os.path.join(DATA_DIR, "sic_payroll.db"),
        "exports": os.path.join(DATA_DIR, "exports"),
        "uploads": os.path.join(DATA_DIR, "uploads"),
        "excel": os.path.join(DATA_DIR, "Payroll Workbook - Shan Ibrahim & Co.xlsx"),
        "port": 8080,
        "officers": ("", "", ""),  # preparer, reviewer, approver: names are kept in local_settings.json
        # Where ICAP records the students' training (the first is the default). Payroll stays with this firm either way.
        "training_orgs": ("Alam & Aulakh Chartered Accountants", "Shan Ibrahim & Co. Chartered Accountants"),
        "intern_old_rate": ("2026-11-01", 15000.0),  # internees' full-time stipend before this date (Rs. 22,500 from it)
    },
    "elite": {
        "name": "Elite Financial Consultants Cost & Management Accountants",
        "short": "Elite Financial Consultants",
        "db": os.path.join(DATA_DIR, "companies", "elite", "payroll.db"),
        "exports": os.path.join(DATA_DIR, "companies", "elite", "exports"),
        "uploads": os.path.join(DATA_DIR, "companies", "elite", "uploads"),
        "excel": None,  # starts empty: staff are added through the app
        "port": 8081,
        "officers": ("", "", ""),
    },
}
DEFAULT_COMPANY = os.environ.get("PAYROLL_COMPANY") or "sic"

# People's names stay in this file on the computer running the app; it is never uploaded with the code.
LOCAL_SETTINGS_PATH = os.path.join(DATA_DIR, "local_settings.json")
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
    DB_PATH, EXPORT_DIR, UPLOAD_DIR, EXCEL_SOURCE = c["db"], c["exports"], c["uploads"], c["excel"]
    PORT = int(os.environ.get("PAYROLL_PORT") or c["port"])
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

use_company(DEFAULT_COMPANY)
