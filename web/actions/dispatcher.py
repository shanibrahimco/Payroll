"""Finds the code for each submitted form."""

from datetime import datetime

from auth.roles import ROLE_APPROVER, ROLE_PREPARER
from common.errors import ChangeError
from common.settings import get_settings_dict

from web.actions import staff as staff_actions
from web.actions import leaves as leaves_actions
from web.actions import attendance as attendance_actions
from web.actions import payroll as payroll_actions
from web.actions import loans as loans_actions
from web.actions import travel as travel_actions
from web.actions import timesheets as timesheets_actions
from web.actions import mail as mail_actions
from web.actions import users as users_actions

ACTION_HANDLERS = {
    "/add_staff": staff_actions.handle,
    "/update_staff": staff_actions.handle,
    "/add_input": staff_actions.handle,
    "/add_loan": staff_actions.handle,
    "/settle_security": staff_actions.handle,
    "/upload_document": staff_actions.handle,
    "/save_doc_type": staff_actions.handle,
    "/toggle_doc_type": staff_actions.handle,
    "/approve_request": staff_actions.handle,
    "/reject_request": staff_actions.handle,
    "/leave_import_upload": leaves_actions.handle,
    "/leave_import_apply": leaves_actions.handle,
    "/leave_import_discard": leaves_actions.handle,
    "/add_holiday": leaves_actions.handle,
    "/delete_holiday": leaves_actions.handle,
    "/email_holidays": leaves_actions.handle,
    "/apply_credit": leaves_actions.handle,
    "/review_credit": leaves_actions.handle,
    "/add_credit": leaves_actions.handle,
    "/decide_credit": leaves_actions.handle,
    "/decide_leave": leaves_actions.handle,
    "/bulk_leave": leaves_actions.handle,
    "/add_leave": leaves_actions.handle,
    "/upload_attendance": attendance_actions.handle,
    "/map_attendance": attendance_actions.handle,
    "/save_location": attendance_actions.handle,
    "/remove_location": attendance_actions.handle,
    "/set_mobile_start": attendance_actions.handle,
    "/review_punch": attendance_actions.handle,
    "/absence_action": attendance_actions.handle,
    "/save_part_hours": attendance_actions.handle,
    "/save_timing": attendance_actions.handle,
    "/delete_timing": attendance_actions.handle,
    "/release_late_hold": attendance_actions.handle,
    "/import_payroll_upload": payroll_actions.handle,
    "/import_payroll_apply": payroll_actions.handle,
    "/import_payroll_discard": payroll_actions.handle,
    "/prepare_month": payroll_actions.handle,
    "/review_month": payroll_actions.handle,
    "/approve_month": payroll_actions.handle,
    "/send_back_month": payroll_actions.handle,
    "/release_salary": payroll_actions.handle,
    "/apply_loan": loans_actions.handle,
    "/export_loans": loans_actions.handle,
    "/review_loan": loans_actions.handle,
    "/decide_loan": loans_actions.handle,
    "/withdraw_loan": loans_actions.handle,
    "/record_loan_repayment": loans_actions.handle,
    "/set_travel_rate": travel_actions.handle,
    "/set_rate_uplift": travel_actions.handle,
    "/fetch_fuel_prices": travel_actions.handle,
    "/add_fuel_price": travel_actions.handle,
    "/add_travel": travel_actions.handle,
    "/review_travel": travel_actions.handle,
    "/withdraw_travel": travel_actions.handle,
    "/mark_invoiced": travel_actions.handle,
    "/save_timesheet": timesheets_actions.handle,
    "/set_ts_access": timesheets_actions.handle,
    "/save_ts_code": timesheets_actions.handle,
    "/decide_timesheet": timesheets_actions.handle,
    "/set_approver_email": mail_actions.handle,
    "/email_all_staff": mail_actions.handle,
    "/upload_emails": mail_actions.handle,
    "/set_email": mail_actions.handle,
    "/save_mail_account": mail_actions.handle,
    "/set_app_url": mail_actions.handle,
    "/send_test_email": mail_actions.handle,
    "/send_logins": mail_actions.handle,
    "/set_delegate": users_actions.handle,
    "/remove_delegate": users_actions.handle,
    "/change_password": users_actions.handle,
    "/add_user": users_actions.handle,
    "/delete_user": users_actions.handle,
}


class ActionsMixin:
    def handle_change(self, conn, path, data, ym, pm_str, user, role, own_emp):
        """Carry out one POSTed action (role already checked against ROLE_ACTIONS); returns a FLASH_MESSAGES key or raises ChangeError."""
        cfg = get_settings_dict(conn)
        cutoff = int(float(cfg.get("cutoff_day", 25)))
        km_rate = float(cfg.get("travel_rate_per_km", 17))
        now = datetime.now().isoformat(timespec="seconds")
        enters_for_others = role in (ROLE_APPROVER, ROLE_PREPARER)
        handler = ACTION_HANDLERS.get(path)
        msg = handler(self, conn, path, data, ym, pm_str, user, role, own_emp, cfg, cutoff, km_rate, now, enters_for_others) if handler else None
        if msg is not None:
            return msg
        raise ChangeError("bad_input")
