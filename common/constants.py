"""Fixed values shared across the system: staff and leave types, timesheet headings, upload limits."""

STUDY_LEAVE_TYPE = "Exam / Study"  # never deducted from salary

ABSENCE_LEAVE_TYPE = "Unapproved absence"  # recorded by the Preparer/Approver; withholds the month's salary

EOBI_ONLY_TYPE = "EOBI Only"  # not an employee (e.g. partners, family): the firm pays their EOBI, nothing else

NOC_TYPE = "NOC Article"  # articleship transferred from another firm: residual leaves and residual period only

ARTICLE_TYPES = ("ICAP Article", NOC_TYPE)

STAFF_TYPE_OPTIONS = (("Permanent", "Permanent"), ("ICAP Article", "ICAP Articleship"), (NOC_TYPE, "NOC Articleship (transferred)"), ("Intern", "Internee"),
                      (EOBI_ONLY_TYPE, "EOBI Only (not an employee)"))

URGENT_WORK_PK = "Urgent work – within Pakistan"

URGENT_WORK_ABROAD = "Urgent work – outside Pakistan"

LEAVE_TYPES = ("Sick", STUDY_LEAVE_TYPE, URGENT_WORK_PK, URGENT_WORK_ABROAD, ABSENCE_LEAVE_TYPE)  # older records may still say Casual etc.

STAFF_LEAVE_TYPES = tuple(t for t in LEAVE_TYPES if t != ABSENCE_LEAVE_TYPE)  # what staff can apply for

NOT_DEDUCTED_TYPES = (STUDY_LEAVE_TYPE, URGENT_WORK_ABROAD)  # count towards the balance but are never deducted from salary

DEFAULT_DOC_TYPES = (  # code, name, applies to, mandatory, has expiry
    ("cnic", "CNIC (scanned, both sides)", "All", 1, 1),
    ("qualification", "Latest qualification document", "All", 1, 0),
    ("cv", "CV", "All", 1, 0),
    ("icap_card", "ICAP student card", "ICAP Article", 1, 0),
)

CNIC_WARN_DAYS = 60  # warn this many days before a CNIC expires

CREDIT_REASONS = {  # why extra leave was earned; ICAP students only for a gazetted holiday their client didn't allow
    "holiday_client": "Worked at client on gazetted holiday (client didn't allow it)",
    "holiday_called": "Called in to work on gazetted holiday",
    "weekend_called": "Called in to work on Saturday / Sunday",
}

TS_DAY_TYPES = ("Work", "Weekend", "Public Holiday", "Study Leave", "Leave", "Absent")

TS_OFF_DAYS = ("Weekend", "Public Holiday")  # not part of the standard hours

TS_TEXT_FIELDS = {  # timesheet_entries column -> daily diary heading
    "client_name": "Client's Name", "period_ended": "Year / Period Ended", "nature_of_work": "Nature of Work Undertaken / Training Received",
    "line_of_service": "Line of Service", "head_of_account": "Head of Account", "work_performed": "Work Performed", "job_code": "Job Code",
}

TS_CODE_FIELDS = ("line_of_service", "head_of_account", "work_performed")  # chosen from the working legends drop-downs

TS_DEFAULT_CODES = {  # codes listed on the daily diary template; the reviewer adds more and their descriptions
    "line_of_service": ("1100", "1200", "1300", "1400", "1500", "1600", "1610", "1620", "1630", "1640", "1650", "1660", "1670"),
    "head_of_account": ("2000", "2010", "2050", "2100", "2200", "2210", "2250", "2300", "2400", "2500", "2600", "2610", "2620",
                        "2630", "2640", "2650", "2660", "2670", "2680", "2700"),
    "work_performed": ("3000", "3100", "3200", "3300", "3400", "3500", "3600", "3700"),
}

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

DEFAULT_OFF_DAYS = "5,6"  # Saturday and Sunday

MIN_PASSWORD_LEN = 8

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

SCAN_TYPES = ((b"%PDF", "application/pdf", "pdf"), (b"\x89PNG", "image/png", "png"), (b"\xff\xd8\xff", "image/jpeg", "jpg"))

MAX_FORM_BYTES = 45 * 1024 * 1024  # a form may carry several documents of up to MAX_UPLOAD_BYTES each
