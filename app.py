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

import config
from common.dates import current_month
from config import COMPANIES, DEFAULT_COMPANY, DEFAULT_HOST, use_company
from database import init_db, set_public_url
from payroll.calculator import calculate_payroll, recalc_open_months
from payroll.seed import seed_from_excel
from travel.fuel import fuel_price_updater
from web.handler import run_server

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Payroll web app")
    parser.add_argument("--company", choices=sorted(COMPANIES), default=DEFAULT_COMPANY,
                        help="which company's payroll to open (each has its own database)")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="address to listen on (default: this computer only; 0.0.0.0 lets phones on the network connect)")
    args = parser.parse_args()
    use_company(args.company)
    init_db()
    seed_from_excel()
    if config.PUBLIC_URL:
        set_public_url(config.PUBLIC_URL)
    recalc_open_months()  # also marks staff whose leaving date has passed as Left
    fuel_price_updater()  # keeps the PSO petrol price (and so the travel rate) current
    calculate_payroll(f"{current_month()}-01")
    print(f"Starting {config.FIRM_NAME} Payroll Web App on http://{args.host}:{config.PORT} ...")
    print(f"Database: {config.DB_PATH}")
    run_server(args.host, config.PORT)
