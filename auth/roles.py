"""Roles, and what each role may open and do."""

ROLE_APPROVER = "Approver"  # approves staff/pay changes and each month's payroll

ROLE_REVIEWER = "Reviewer"  # reviews the prepared payroll; approves or rejects leave applications

ROLE_PREPARER = "Preparer"  # enters data and prepares the payroll; pay changes wait for approval

ROLE_STAFF = "Staff"        # applies for own leave and submits own travel claims, nothing else

ROLES = (ROLE_APPROVER, ROLE_REVIEWER, ROLE_PREPARER, ROLE_STAFF)

MOBILE_ROLES = (ROLE_REVIEWER, ROLE_PREPARER, ROLE_STAFF)  # everyone but the approver checks in on the mobile app

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
    "/upload_attendance": (), "/map_attendance": (),  # attendance is by mobile check-in only: nobody can enter days from a report
    "/absence_action": (ROLE_PREPARER, ROLE_APPROVER),
    "/save_location": (ROLE_REVIEWER, ROLE_APPROVER), "/remove_location": (ROLE_REVIEWER, ROLE_APPROVER),  # approved check-in locations
    "/set_mobile_start": (ROLE_REVIEWER, ROLE_APPROVER), "/review_punch": (ROLE_REVIEWER, ROLE_APPROVER),  # check-ins made elsewhere
    "/save_timing": (ROLE_REVIEWER, ROLE_APPROVER), "/delete_timing": (ROLE_REVIEWER, ROLE_APPROVER),  # individual office timings
    "/save_part_hours": (ROLE_REVIEWER, ROLE_APPROVER),  # part-timers' weekly hours, updated each month
    "/settle_security": (ROLE_APPROVER,),  # held pay / security of a leaver: only the approver releases or forfeits it
    "/leave_import_upload": (ROLE_APPROVER,), "/leave_import_apply": (ROLE_APPROVER,), "/leave_import_discard": (ROLE_APPROVER,),
    "/import_payroll_upload": (ROLE_APPROVER,), "/import_payroll_apply": (ROLE_APPROVER,), "/import_payroll_discard": (ROLE_APPROVER,),
    "/save_doc_type": (ROLE_APPROVER,), "/toggle_doc_type": (ROLE_APPROVER,),  # only the approver decides required documents  # who operates the portal of staff who can't
}
