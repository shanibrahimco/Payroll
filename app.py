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
from http.server import HTTPServer

import config
from config import COMPANIES, DEFAULT_COMPANY, use_company
from database import init_db
from payroll.calculator import calculate_payroll, recalc_open_months
from payroll.seed import seed_from_excel
from travel.fuel import fuel_price_updater
from web.handler import PayrollHandler

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Payroll web app")
    parser.add_argument("--company", choices=sorted(COMPANIES), default=DEFAULT_COMPANY,
                        help="which company's payroll to open (each has its own database)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default: this computer only; 0.0.0.0 lets phones on the network connect)")
    args = parser.parse_args()
    use_company(args.company)
    init_db()
    seed_from_excel()
    recalc_open_months()  # also marks staff whose leaving date has passed as Left
    fuel_price_updater()  # keeps the PSO petrol price (and so the travel rate) current
    calculate_payroll("2026-11-01")
    print(f"Starting {config.FIRM_NAME} Payroll Web App on http://{args.host}:{config.PORT} ...")
    print(f"Database: {config.DB_PATH}")
    HTTPServer((args.host, config.PORT), PayrollHandler).serve_forever()
