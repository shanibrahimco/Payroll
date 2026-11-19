"""Travel claims, rates and petrol prices."""

from datetime import date
import os
import secrets

import config
from auth.roles import ROLE_REVIEWER
from common.dates import fmt_date, get_payroll_month_for_event, parse_date
from common.errors import ChangeError
from common.settings import log_decision
from leaves.rules import check_entry
from notifications.mailer import notify_approver
from staff.documents import check_document
from travel.claims import OWN_VEHICLE, TRAVEL_MODES, review_travel
from travel.fuel import fetch_fuel_prices, km_rate_from_price, petrol_price_on, travel_rate_on

def handle(self, conn, path, data, ym, pm_str, user, role, own_emp, cfg, cutoff, km_rate, now, enters_for_others):
    if path == "/set_travel_rate":
        # the formula: Rs. <rate> per km when petrol (Premier Euro 5) costs Rs. <base_price> a litre
        rate = float(data.get("rate") or 0)
        base_price = float(data.get("base_price") or cfg.get("travel_rate_base_price", 400))
        if rate <= 0 or base_price <= 0:
            raise ChangeError("rate_invalid")
        old = f"Rs. {float(cfg.get('travel_rate_base_km', 17)):g}/km at Rs. {float(cfg.get('travel_rate_base_price', 400)):g}/litre"
        for key, value in (("travel_rate_base_km", rate), ("travel_rate_base_price", base_price), ("travel_rate_per_km", rate)):
            conn.execute("UPDATE settings SET value = ? WHERE key = ?", (f"{value:g}", key))
        log_decision(conn, "travel_rate", None, None, {"base_km": rate, "base_price": base_price},
                     f"Travel rate formula {old} → Rs. {rate:g}/km at Rs. {base_price:g}/litre (claims entered from now on)", user)
        return "rate_updated"

    if path == "/set_rate_uplift":
        # Reviewer: raise the formula rate by at most 10% (0 = back to the formula), from today, with a written reason.
        # Approver: any uplift, from any date.
        pct = round(float(data.get("pct") or 0), 2)
        reason = data.get("reason", "").strip()[:300]
        start = date.today() if role == ROLE_REVIEWER else (parse_date(data.get("effective_from")) or date.today())
        limit = float(cfg.get("travel_rate_max_uplift_pct", 10)) if role == ROLE_REVIEWER else 100.0
        if not 0 <= pct <= limit:
            raise ChangeError("uplift_too_high")
        if len(reason) < 10:
            raise ChangeError("uplift_need_reason")
        conn.execute("INSERT INTO rate_uplifts (effective_from, pct, reason, set_by, set_at) VALUES (?,?,?,?,?)",
                     (fmt_date(start), pct, reason, user, now))
        formula_rate = km_rate_from_price(petrol_price_on(conn, start)[1], cfg)
        summary = (f"Travel rate uplift {pct:g}% from {start.strftime('%d %b %Y')}: formula Rs. {formula_rate:g}/km → "
                   f"Rs. {travel_rate_on(conn, start, cfg):g}/km. Reason: {reason}")
        log_decision(conn, "travel_rate", None, None, {"pct": pct, "from": fmt_date(start), "reason": reason}, summary, user)
        if role == ROLE_REVIEWER:
            notify_approver(conn, user, f"{config.FIRM_SHORT} payroll — travel rate increased by {pct:g}%",
                            f"{user} set a travel rate uplift:\n\n  {summary}\n\nYou can change it on the Travel Claims tab: {cfg.get('app_url', '')}\n")
        return "uplift_saved"

    if path == "/fetch_fuel_prices":
        try:
            added = fetch_fuel_prices(conn, pages=2)
        except Exception as e:
            conn.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('fuel_price_last_check', ?, 'Last PSO price check')",
                         (f"{now[:16]} Failed — {type(e).__name__}: {str(e)[:150]}",))
            conn.commit()
            raise ChangeError("fuel_fetch_failed")
        conn.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('fuel_price_last_check', ?, 'Last PSO price check')",
                     (f"{now[:16]} OK — {added} new price(s)",))
        return "fuel_fetched"

    if path == "/add_fuel_price":
        d, price = parse_date(data.get("effective_date")), float(data.get("price") or 0)
        if d is None or price <= 0 or d > date.today():
            raise ChangeError("fuel_price_invalid")
        conn.execute("INSERT OR REPLACE INTO fuel_prices (effective_date, price, source, recorded_by, recorded_at) VALUES (?,?,'Manual',?,?)",
                     (fmt_date(d), price, user, now))
        log_decision(conn, "travel_rate", None, None, {"date": fmt_date(d), "price": price},
                     f"Petrol (Premier Euro 5) price entered by hand: Rs. {price:g}/litre from {fmt_date(d)} → Rs. {travel_rate_on(conn, d, cfg):g}/km", user)
        return "fuel_price_saved"

    if path == "/add_travel":
        emp_id = data["emp_id"].strip().upper() if enters_for_others else own_emp
        if not emp_id:
            raise ChangeError("no_emp_link")
        c_d = parse_date(data["claim_date"])
        if c_d is None or c_d > date.today():
            raise ChangeError("claim_future")
        pm_target = fmt_date(get_payroll_month_for_event(c_d, cutoff))
        check_entry(conn, emp_id, pm_target)
        mode = data.get("travel_mode") or OWN_VEHICLE
        if mode not in TRAVEL_MODES:
            raise ChangeError("bad_input")
        kms = float(data.get("kms") or 0)
        park = float(data.get("parking") or 0)
        other = float(data.get("other_charges") or 0)
        fare = float(data.get("fare") or 0)
        if mode == OWN_VEHICLE:  # kms x the petrol-linked rate on the day travelled
            km_rate, fare = travel_rate_on(conn, c_d, cfg), 0.0
            tot = round(kms * km_rate + park + other, 0)
        else:  # ride service: the actual fare, reimbursed once the reviewer approves it
            km_rate = 0.0
            if fare <= 0:
                raise ChangeError("claim_need_fare")
            tot = round(fare + park + other, 0)
        if tot <= 0 or kms < 0 or park < 0 or other < 0:
            raise ChangeError("bad_input")
        receipt = self.files.get("receipt")
        receipt_cols = (None, None, None)
        if receipt and receipt[2]:
            name, content, mime, ext = check_document(receipt)
            folder = os.path.join(config.UPLOAD_DIR, "receipts")
            os.makedirs(folder, exist_ok=True)
            stored = f"receipt_{emp_id}_{c_d.isoformat()}_{secrets.token_hex(8)}.{ext}"
            with open(os.path.join(folder, stored), "wb") as f:
                f.write(content)
            receipt_cols = (stored, name, mime)
        conn.execute("""
                INSERT INTO travel_claims (claim_date, emp_id, client_name, venue_particular, kms, rate_per_km, parking, other_charges, total_amount,
                                           payroll_month, status, submitted_by, travel_mode, fare, receipt_file, receipt_name, receipt_type)
                VALUES (?,?,?,?,?,?,?,?,?,?,'Submitted',?,?,?,?,?,?)
            """, (data["claim_date"], emp_id, data.get("client_name",""),
              data.get("venue_particular",""), kms, km_rate, park, other, tot, pm_target, user, mode, fare, *receipt_cols))
        return "claim_submitted"

    if path in ("/review_travel", "/withdraw_travel"):
        claim = conn.execute("SELECT * FROM travel_claims WHERE id = ?", (int(data["id"]),)).fetchone()
        if claim is None:
            raise ChangeError("bad_input")
        if path == "/review_travel":
            return review_travel(conn, claim, data.get("decision"), data.get("chargeable") == "1",
                                 data.get("client_name", ""), data.get("note", ""), user, own_emp, role)
        if claim["status"] != "Submitted":
            raise ChangeError("claim_wrong_stage")
        if not enters_for_others and claim["emp_id"] != own_emp:
            raise ChangeError("not_allowed")
        conn.execute("DELETE FROM travel_claims WHERE id = ?", (claim["id"],))
        return "claim_withdrawn"

    if path == "/mark_invoiced":
        client, month = data.get("client_name", ""), data.get("payroll_month", "")
        cur = conn.execute("""
                UPDATE travel_claims SET invoiced = 1, invoice_no = ?, invoiced_by = ?, invoiced_at = ?
                WHERE status = 'Reviewed' AND chargeable = 1 AND invoiced = 0 AND client_name = ? AND payroll_month = ?
                  AND payroll_month IN (SELECT payroll_month FROM payroll_runs WHERE status = 'Approved')
            """, (data.get("invoice_no", "").strip()[:40], user, now, client, month))
        if cur.rowcount == 0:
            raise ChangeError("invoice_invalid")
        return "invoiced"
