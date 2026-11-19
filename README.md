# Shan Ibrahim & Co. Chartered Accountants — Payroll System (Python + SQLite)

## Quick Start in VS Code
1. Extract this ZIP folder and open the folder **`Shan_Ibrahim_Payroll_VSCode`** in **VS Code** (`File > Open Folder...`).
2. Open **`app.py`** and press **`F5`** (or click the **Run Python File** ▶ button at the top right).
3. Open your browser to **`http://127.0.0.1:8080`**.

## Project structure

| Location | What it holds |
|---|---|
| `app.py` | Starts the server |
| `config.py` | Company settings, ports and folders |
| `database.py` | Database tables, connection and upgrades |
| `payroll/` | Salary calculation, bank files, change approvals, final sheet import |
| `attendance/` | Mobile check-in, approved locations, office timings, absences |
| `leaves/`, `travel/`, `loans/`, `timesheets/`, `staff/` | The rules for each area |
| `auth/` | Roles, logins, sessions and the login limit |
| `notifications/` | Emails |
| `common/` | Dates, messages, settings and fixed values used everywhere |
| `web/` | Web app: `router.py`, one file per tab in `pages/`, form handling in `actions/` |
| `api/` | Mobile app API (`/api/*`) |
| `tests/` | Automated checks: `python tests/test_app.py` (runs on a temporary copy of the data) |

## Companies
Each company has its own database, users, exports folder and port. One firm's staff and payroll are never visible to the other.

| Company | Start with | Address | Database |
|---|---|---|---|
| Shan Ibrahim & Co. Chartered Accountants | `python app.py` | `http://127.0.0.1:8080` | `sic_payroll.db` |
| Elite Financial Consultants Cost & Management Accountants | `python app.py --company elite` | `http://127.0.0.1:8081` | `companies/elite/payroll.db` |

In VS Code, pick the company from the Run and Debug drop-down (**Run SIC Payroll System** / **Run Elite Financial Consultants Payroll**).
A new company's first visit opens the one-time setup page, where its own administrator (Approver) account is created.
To add another company, add an entry to `COMPANIES` in `config.py`.

## Approval Controls
- **Roles:** the first account created is the **Approver**. The Approver adds other users on the Users tab:
  - **Approver** — approves staff/pay changes and each month's payroll.
  - **Reviewer** — sees everything, approves/rejects leave applications, reviews the prepared payroll.
  - **Preparer** — enters data and prepares the payroll; pay changes wait for approval.
  - **Staff** — can only apply for their own leave and submit their own travel claims (login linked to their Emp ID).
- **Monthly sign-off:** Draft → **Prepared** (Preparer) → **Reviewed** (Reviewer) → **Approved** (Approver). From Prepared onward the month is locked; the Reviewer (or the Approver, once approved) can send it back to Draft.
- **Leave chain:** every leave is recorded as *Pending* → **Recommended** by the Reviewer → **Approved** by the Approver (either may reject). Nobody decides their own leave; the Reviewer's own leave goes straight to the Approver. Bulk *Recommend All* / *Approve All* buttons are on the Leave Register.
- **Unapproved absence:** recorded by the Preparer or Approver (type *Unapproved absence*). It **withholds the employee's whole net salary for that month**, which stays withheld until the Approver releases it into a later Draft month (Withheld Salaries panel on Monthly Payroll). A record entered by mistake can be cancelled by the Reviewer or Approver before the month is prepared.
- **Travel claims:** staff submit their own claims; each goes to the **Reviewer**, who marks it *Reviewed* (and chooses **Chargeable to client (OPE)** or not, correcting the client name if needed) or *Rejected* with a reason. **Only reviewed claims are paid.** A claim reviewed after its month was prepared is paid in the next open month. Nobody reviews their own claim (the Approver reviews the Reviewer's). A submitted claim can be withdrawn by its owner (or the Preparer/Approver) before review.
- **Chargeable travel reminder:** once a payroll is approved, its chargeable claims appear in the *To Be Invoiced* report on the Travel Claims tab, grouped by client, and a **reminder pops up for the Reviewer** on every page until each client's amount is marked *Included in Invoice* (with the invoice no.).
- **Travel rate linked to petrol:** the per-km rate follows the **PSO Premier Euro 5** price, read automatically from psopk.com every 6 hours (`fuel_price_url`): **rate = petrol price × 17 ÷ 400** (Rs. 17/km at Rs. 400/litre; `travel_rate_base_km`, `travel_rate_base_price`). Each claim uses the price in force on the **date travelled** and keeps that rate. The Travel Claims tab shows everyone today's petrol price, the formula rate, any increase (with its reason) and the final rate, plus recent prices. With no price on file, `travel_rate_per_km` is used.
- **Ride services:** a claim can be *Travelled By* **own vehicle** (kms × the petrol-linked rate) or **InDrive, Yango, Cab / Careem / Uber, Auto rickshaw or Bike ride service**, which is reimbursed at the **actual fare** entered (plus parking / other). A receipt or app screenshot can be attached (PDF/JPG/PNG). Like every claim, it is paid only after the Reviewer approves it.
- **Who controls the rate:** the **Reviewer** may only **increase** the rate by at most **10%** above the formula (`travel_rate_max_uplift_pct`), from today, with a **written reason** — the Approver is notified; 0% returns to the formula. **All other powers rest with the Approver**: the formula, any larger or backdated increase, and hand-entered petrol prices. Either can fetch the PSO prices at once. Everything is in the Approval Log.
- **Travel rate (older note):** the Reviewer or Approver can change the per-km rate on the Travel Claims tab. A new rate applies to claims entered after the change; every change is in the Approval Log.
- **Nothing that changes pay applies without the Approver.** When a Preparer adds a new staff member, changes staff/pay details (salary, hours, type, status, joining/EOBI/ICAP dates), enters a monthly adjustment (bonus, arrears, deductions, waived leaves) or an advance/loan, it becomes a request on the **Approvals** tab and changes nothing until approved. The Approver's own changes apply at once. Every change is kept in the Approval Log.
- **Monthly approval & lock:** each month's payroll must be approved on the Monthly Payroll tab before bank files can be exported. An approved month is **locked**: its figures are never recalculated and no leaves, travel or adjustments can be entered for it until the Approver reopens it.
- **Excel import:** the workbook only seeds an empty database; it is never re-imported over existing staff.

## Pay Release Policy
- **Permanent staff:** regular monthly pay.
- **Internees:** nothing is paid for the first 4 months (`intern_pay_hold_months`). On completing 4 months the accumulated pay is released except **one month's pay, kept as security**; regular pay follows. Security comes only from pay still being held: pay already released is never taken back as security.
- **Internee stipend:** the full-time rate was **Rs. 15,000 up to 31 October 2026** and is **Rs. 22,500 from 1 November 2026**.
- **Part-timers:** pay = full-time rate × weekly hours ÷ 45. The **Reviewer** updates each part-timer's weekly hours at the start of every month (*Part-Timer Hours* on the Attendance tab). Hours carry forward until changed, and every update is in the Approval Log.
- **ICAP and NOC students:** the stipend accumulates until the **ICAP registration card is received** (*ICAP Card Received* on Staff Master), then the whole amount is paid. No security.
- **Leavers:** the *Held Pay & Security* panel on Monthly Payroll lists every held amount. Only the **Approver** settles a leaver's balance: **Release** (after confirming proper notice and handing / taking over) or **Forfeit** (with a reason). Each settlement is in the Approval Log.
- **NOC students** (staff type *NOC Article*): transferred articleship with **residual leaves** and a **residual period end date** instead of 130 leaves over 3 years 6 months. Otherwise they follow the ICAP rules (timesheets, EOBI, documents).
- **ICAP Training Organisation:** all ICAP and NOC students are paid by Shan Ibrahim & Co., while ICAP records their training under **Alam & Aulakh Chartered Accountants** (the default). *Shan Ibrahim & Co. Chartered Accountants* stays selectable on Staff Master for when the firm is registered as a training organisation. The selected firm heads the student's printed Daily Diary; it has no effect on pay.

## Late Arrivals
- **No deduction:** late arrivals are never deducted from pay.
- **Office timings (Attendance tab):** everyone follows the firm's timing (09:00 to 18:00, Saturday and Sunday off) unless the **Reviewer** or **Approver** sets a person's own timing: start and end time, days off, from and to dates, and a reason. A dated timing (for example one month) ends by itself. The Reviewer cannot set his own timing. Timings decide lateness, off days and absences; they do not change pay. Every change is in the Approval Log.
- **Late** means arriving more than **15 minutes** (`late_grace_minutes`) after the person's start time, read from the arrival times in the attendance report.
- **Salary hold:** on **3 late arrivals in one calendar month** (`late_hold_days`), counted from 26 November 2026 (`late_hold_from`), that month's whole net salary or stipend is held. They need not be on consecutive days, and the count starts again each month. The person's days off and gazetted holidays are not counted.
- **Release:** only the **Approver** releases the hold, under *Salaries Held for Late Arrivals* on Monthly Payroll: into the same month while it is Draft, otherwise into a later Draft month. Each release is in the Approval Log.

## Access of Staff Who Leave
A login stops working automatically after the person's **leaving date** (or as soon as they are marked *Left* if no date is recorded), including any session already open. Their records stay in the system, and the Users tab shows the access as removed.

## Leave History Upload (Leave Register tab — Approver only)
- *Download the Leave History template* (a copy is `templates/Leave Record - Shan Ibrahim & Co.xlsx`): **Opening Balance** as at 1 Jan 2026 typed in by hand (pre-filled staff list); **Leaves**, one leave per row with the **Employee Name and Leave Type chosen from drop-downs**, Emp ID and Days filled in by formula (overwrite Days with 0.5 for half a day); and a **Summary** that keeps each person's balance by formula.
- **Leave types** (staff applications, the template and the Attendance tab): **Sick, Exam / Study, Urgent work – within Pakistan, Urgent work – outside Pakistan** (older records keep their original type, e.g. Casual). All count towards the balance; **Exam / Study and Urgent work – outside Pakistan are never deducted**, the others are deducted beyond 2 leaves a month. The preparer records unapproved absences separately.
- Upload → **preview** (per person: opening balance, 2026 leave rows / days / study days, records already in the system, rows that will be skipped and why) → **Apply**. Opening balances become “as at 31 Dec 2025”; leaves are recorded as **approved** by the Approver; optionally the staff's earlier 2026 records are replaced (recorded absences kept). Re-applying the same file adds nothing twice. Logged in the Approval Log.

## Attendance (Attendance tab) — the payroll starts here
- The **Preparer** (or Approver) uploads the attendance software's *Monthly Performance Report* (PDF; read with `pypdf`, see requirements.txt). Each person's daily status (P, P-LT late, A absent, WO weekly off …) is saved by date; a later report for the same month updates the days it covers.
- People are matched by their attendance **EmpCode** (remembered after the first match by exact name); anyone not matched is listed with a *Match* form. Staff on the payroll missing from the report are listed.
- For each person: present, late, absent, absences covered by a leave application or gazetted holiday, and **absences with no leave filed**. The Preparer files those as leave (*File as Leave*: Pending → Reviewer recommends → Approver approves, deducted under the leave rules) or records them as **Unapproved Absence** (whole month's salary withheld until the Approver releases it).
- The month **can't be marked as Prepared until its attendance is uploaded**. Late arrivals are shown, not deducted automatically (enter any late deduction in Monthly Inputs).
- Note: the report's own "Absent" total can include the day it was printed; the system uses the dated day-by-day statuses.

## Final Payroll Sheet Upload (Monthly Payroll tab — Approver only)
- For a month finalised outside the system (**November 2026**: the Reviewer's final Excel sheet of 25 Nov). The Approver uploads the sheet (.xlsx or .csv); columns are recognised by their **headings** (Emp ID or Name, and Net Pay required; Gross, Monthly Salary, Days, Travel, Leave Ded., Loan, Prob. Withheld / Released, Bonus, Arrears, Late, Tax, EOBI used when present). Title rows and a TOTAL row are ignored.
- **Template:** *Download Payroll Sheet Template (Excel)* on Monthly Payroll (Approver, Reviewer, Preparer) gives a sheet pre-filled with that month's payroll staff and the system's current figures, using exactly the recognised headings; Net Pay is a formula (Gross + Travelling + Bonus + Arrears + Prob. Released − Leave Ded. − Late Ded. − Loan Rec. − Prob. Withheld − Tax) and an Instructions sheet explains the rules. If the formulas were never calculated, the upload works Net Pay out the same way. A copy for November 2026 is in `templates/`.
- A **preview** shows the recognised columns, staff matched, sheet rows matching nobody, staff on the payroll but missing from the sheet (they will be paid nothing), and net pay system vs sheet. Nothing changes until **Apply**.
- Applying replaces the month's figures with the sheet's (optionally also updating staff monthly salaries), **approves and locks** the month and records it in the Approval Log; bank files can be exported. An imported month can't be sent back for recalculation — upload a corrected sheet instead.
- **From 26 November 2026 the system's own rules apply**: December onward is calculated by the system, carrying forward the sheet's withheld / recovered amounts.

## Staff Documents & CNIC Expiry (Staff Master tab)
- **Documents per staff member** (PDF / JPG / PNG, ≤ 10 MB each), kept in `uploads/documents/`: CNIC scan, latest qualification, CV, and (ICAP students) the ICAP student card. *Staff Documents* shows what is on file and what is missing; the Preparer or Approver uploads; a newer copy keeps the older one as history. Officers can open any document, staff only their own.
- **Adding a new staff member** requires every **mandatory** document for their staff type plus the CNIC expiry date; the documents travel with the request (links on the Approvals tab) and are deleted if the request is rejected. EOBI-only people need none.
- **Required documents are set by the Approver** (*Required Documents for New Staff*): add a document type for All / Permanent / ICAP Article / Intern, mark it mandatory or optional, with or without an expiry date, or stop using it.
- **CNIC expiry:** each staff record has the CNIC issue / expiry date. *CNIC Alerts* on Staff Master lists expired CNICs and those expiring within 60 days; an expired CNIC also shows in the payroll checks and as a reminder on the staff member's own portal.

## Portals Operated for Staff
- For staff who can't use the portal. The Approver assigns an operator on the Users tab (*Portals Operated for Staff*).
- The operator clicks *Operate <name>'s Portal* and works in that person's portal (leave, travel, loans, timesheet) until *Return to My Portal*. Everything is recorded as "<operator> for <name>" (e.g. *filed by … for <name>* on leave). The operator can't change their password.
- **Control:** the Reviewer never reviews requests of staff whose portal he operates — their leave, travel claims, loan applications and timesheets go straight to the **Approver**, exactly like the Reviewer's own.

## Loans & Advances (Advances & Loans tab)
- **Every staff member can apply** for a loan or an advance (amount, instalments, purpose). The **Reviewer** recommends or rejects it (with a reason); the **Approver** approves (optionally a lower amount) or rejects it. The Reviewer's own application goes straight to the Approver. An applicant can withdraw an application until it is reviewed.
- **Policy:** loans and advances outstanding (plus applications still pending) may not exceed **2 monthly salaries** (`loan_max_salaries`; part-timers' pro-rated salary), and a loan is repaid in at most **20 monthly instalments** (`loan_max_instalments`). An advance is recovered in full next month.
- **Additional loan above the limit:** the application is flagged *Exceeds policy*; the Reviewer can recommend it only as an **exception with a written justification**, and the Approver may approve it. The approved loan is marked as an exception.
- **Paying out:** approved loans are paid by **separate loan bank files** (Meezan IFT for Meezan accounts, IBFT for others), never with salaries. *Generate Loan Bank Files* on the Advances & Loans tab writes `exports/loans/IBFT_Loans_<batch>.csv` / `Meezan_IFT_Loans_<batch>.csv` (same formats as the salary files) and marks each loan paid with the batch number, so it can't be paid twice. Loans for staff without a bank account / IBFT code are left for the next batch. Loans recorded directly (earlier loans) are treated as already paid.
- On approval a loan record (`LA0001`, …) is created with equal instalments, recovered from the month after approval. The Approver is notified (Approvals tab, and by email if set). Loans entered directly by the Preparer also respect the 20-instalment limit, and amounts above the policy are flagged for the Approver.

## Gazetted Holidays (Gazetted Holidays tab)
The **Reviewer** (or Approver) adds and removes gazetted holidays on their own tab; everyone else can view the list. Adding or removing an upcoming holiday automatically emails the updated list to all active staff from the Reviewer's own email account (set up on the Email tab). If the email cannot be sent, the holiday is still saved and the screen says so; *Email Holiday List Again* resends it. Past dates entered for the record are not emailed.

## Leave Credits (Leave Register tab)
- **Holidays:** the Reviewer (or Approver) keeps the gazetted holiday list and can **email it to all staff** from his own email account. Saturday and Sunday are always off. Holidays fill in automatically on timesheets and are excluded from standard hours; *Public Holiday* can only be chosen on a listed holiday, *Weekend* only on Sat/Sun.
- **ICAP students at clients** follow the client's holidays. If the client didn't allow a gazetted holiday, the student marks that day as *Work* on the timesheet and applies for credit leave like all other staff.
- **All staff, including ICAP / NOC students:** one credit leave for each **complete working day** spent at work on a gazetted holiday, or on a Saturday / Sunday that is the person's day off (a Saturday someone normally works earns no credit). The staff member applies on the Leave Register tab **within 7 days** of the date worked (`credit_apply_days`). The **Reviewer** recommends it and the **Approver** approves it; the credit counts only after approval. A later application moves forward only if the Reviewer **condones the delay** with a written reason. The Reviewer's own applications and those of portals he operates go to the Approver. One credit per person per date.
- **Effect:** an approved credit adds to the leave entitlement (ICAP: whole articleship; others: that calendar year) and covers leave days that would otherwise be deducted (shown as *Credit Used* in the Leave Summary).

## Email (Email tab — Approver, Reviewer, Preparer)
- **Staff emails:** upload a CSV or Excel (.xlsx) list with an *Email* column and an *Emp ID*, *CNIC* or *Name* column; rows are matched to staff and the result (updated / not matched / invalid) is shown. Single addresses can be edited too. Every upload/edit is in the Approval Log.
- **Sending account:** each sender connects their own email account (SMTP). Gmail: smtp.gmail.com, 587, STARTTLS, with a Google *App Password*. Outlook / Microsoft 365: smtp.office365.com, 587, STARTTLS. The app password is stored only in the local database and never displayed. *Send Test Email to Myself* checks it.
- **Login details:** sends each person their user ID (CNIC without dashes), starting password and the payroll address (set *Payroll Address in Emails* when deployed). Only accounts still on their starting password can be sent one (passwords are stored hashed; the starting password is re-applied at sending). Everyone must change it at first login. Every email is in the Email Log (passwords are never logged).

## ICAP Student Timesheets (Daily Diary)
- Every ICAP student submits a monthly timesheet on the **Timesheets** tab: Draft → **Submitted** (student) → **Reviewed** (Reviewer) → **Approved** (Approver). The Reviewer or Approver can return it with a note for correction; the Approver can reopen an approved one while its payroll month is still Draft.
- **Salary is held until the timesheet is approved** (from November 2026, setting `timesheet_required_from`). Approval releases it: in the same month's payroll while that is still Draft, otherwise automatically in the next Draft month (Timesheet Hold column on Monthly Payroll).
- **November 2026:** the paper sheet is scanned and uploaded (PDF/JPG/PNG, up to 10 MB) and the month-end total hours and leave days are typed in; the Reviewer checks them against the scan.
- **From December 2026** (setting `timesheet_online_from`): filled in day by day in the system, in the daily diary layout (client, year/period, nature of work, Line of Service, Head of Account, Work Performed, Job Code, hours). Standard hours = working days × **8 h** (9-hour office day less the 1-hour lunch break; setting `timesheet_hours_per_day`). **Print Daily Diary** gives the sheet with student / reviewer / approver signatures and the meaning of each code used.
- **Working legends:** Line of Service, Head of Account and Work Performed are drop-down lists (seeded with the codes on the diary template). The **Reviewer** (or Approver) adds new codes and their descriptions in the *Working Legends* panel on the Timesheets tab; every addition is in the Approval Log. Job Code stays free text.
- **Only ICAP students** have the Timesheets tab. If the **Reviewer** needs timesheets from another staff member, he enables it in *Timesheets for Other Staff* with a start month; from then the same rules apply (reviewed, approved, salary held until approved) until he marks it *No Longer Required*. Every change is in the Approval Log.
- The student (or the Approver on their behalf) prepares the sheet; the Preparer has no access to timesheets. The reviewer sees leave-register days next to the sheet's leave days as a cross-check. Uploaded scans are kept in `uploads/` (per company).

## Configured Firm Rules
- **Income Tax Deduction:** Disabled (`Rs. 0` deducted from employee salaries).
- **EOBI Contribution:** Calculated on the `Rs. 40,700` wage base — **5% Employer (`Rs. 2,035`) + 1% Employee (`Rs. 407`) = `Rs. 2,442/month` are BOTH borne by the employer** (`Rs. 0` deducted from employee salary).
- **Staff Categories & EOBI Eligibility:**
  - **Permanent** — EOBI starts **3 months after the joining date** (from the payroll month in which the 3 months complete).
  - **ICAP Articleship** — EOBI starts from the **ICAP registration date** (from the payroll month of registration). No EOBI before registration.
  - **Internee** — **no EOBI**.
  - Once a staff member's **EOBI Registration Date** is entered, EOBI is charged from that date (except Internees). Staff who are due EOBI but have no registration date are flagged *EOBI registration pending*.
  - The 3-month waiting period is the `eobi_permanent_wait_months` setting.
- **Leave Rules:**
  - **Permanent & Internees** — 24 leaves per calendar year. On **1 January** the year's count restarts and unused leave (24 + brought forward + approved credits − taken) is **carried forward into a leave bank of at most 40 days** (`leave_bank_max`). Days above 40 are **paid in January's payroll** (*Leave Encash.*, at salary ÷ days in month per day). There is no year-end deduction.
  - **ICAP Articleship** — 130 leaves over the 3 years 6 months tenure (no carry forward / encashment).
  - **Leave approval:** every leave is recommended by the Reviewer and finally approved by the Approver.
  - Everyone gets **2 paid leaves per month**; days beyond that are deducted from salary (`salary ÷ days in month × excess days`).
  - **Study leave** (type *Exam / Study*) is never deducted from salary, but still counts towards the leave balance.
- **Working Hours & Part-Timers:** Full time is **9 hours/day, 45 hours/week** (`standard_weekly_hours`). Enter the full-time monthly rate as the salary; for a part-timer also enter **Weekly Hours**, and the salary is paid pro-rata (`salary × weekly hours ÷ 45`).
- **Travelling Allowance Rate:** Set to **`Rs. 17` per km** (`Kms × 17 + Parking + Other`).
- **Cut-off Date:** Any leave application or travel claim dated after the **25th** automatically falls into the next month's payroll.
- **Database:** Pre-seeded SQLite database **`sic_payroll.db`** (includes all 17 staff members, opening leave balances, probation withholdings, and loans `L001`–`L006`). You can inspect `sic_payroll.db` directly inside VS Code using the *SQLite Viewer* extension.
