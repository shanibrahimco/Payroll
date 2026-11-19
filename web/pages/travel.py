"""The travel tab of the web app."""

from datetime import date
import html as html_lib

from auth.roles import ROLE_ACTIONS, ROLE_APPROVER, ROLE_PREPARER, ROLE_REVIEWER
from common.dates import parse_date, pay_month_label
from common.messages import FLASH_MESSAGES
from common.settings import get_settings_dict
from travel.claims import OWN_VEHICLE, TRAVEL_MODES, chargeable_to_invoice
from travel.fuel import km_rate_from_price, petrol_price_on, rate_uplift_on

def render(self, cannot_decide, conn, is_staff, km_rate, own_emp, pm_str, role, ym, body=""):
    only_emp = (own_emp or "") if is_staff else None  # Staff see only their own records
    trs = conn.execute("""SELECT t.*, s.employee_name FROM travel_claims t JOIN staff s ON t.emp_id=s.emp_id
                                  WHERE ? IS NULL OR t.emp_id = ? ORDER BY t.id DESC""", (only_emp, only_emp)).fetchall()
    esc = html_lib.escape
    chain_note = ("Every claim goes to the <b>reviewer</b>, who marks it reviewed and decides whether it is an out-of-pocket expense "
                  "<b>chargeable to the client</b>. A claim is paid in payroll only once reviewed.")
    if role in (ROLE_APPROVER, ROLE_PREPARER):
        travel_who, travel_note = '<label>Emp ID <input name="emp_id" placeholder="E008" required size="6"></label>', chain_note
    else:
        travel_who = f'<input type="hidden" name="emp_id" value="{own_emp or ""}">'
        travel_note = (f"You can submit your own travel claims only. {chain_note}" if own_emp else FLASH_MESSAGES["no_emp_link"][1])
    can_review = role in ROLE_ACTIONS["/review_travel"]

    def claim_status(t):
        st = t["status"] or "Submitted"
        badge = f"<span class='badge {'b-ok' if st == 'Reviewed' else 'b-warn'}'>{st}</span>"
        if st == "Reviewed":
            badge += f"<br><span class='note'>by {esc(t['reviewed_by'] or '')}</span>"
        if st == "Rejected":
            badge += f"<br><span class='note'>{esc(t['review_note'] or '')}</span>"
        return badge

    def claim_charge(t):
        if t["status"] != "Reviewed":
            return "—"
        if not t["chargeable"]:
            return "Firm expense"
        inv = (f"<br><span class='badge b-ok'>Invoiced{' ' + esc(t['invoice_no']) if t['invoice_no'] else ''}</span>" if t["invoiced"]
               else "<br><span class='note'>not yet invoiced</span>")
        return f"<b>Chargeable (OPE)</b>{inv}"

    def claim_action(t):
        if t["status"] != "Submitted":
            return ""
        out = ""
        if can_review and not cannot_decide(t["emp_id"]):
            out += (f"<form method='POST' action='/review_travel?ym={ym}' style='display:flex; gap:4px; align-items:center;'>"
                    f"<input type='hidden' name='id' value='{t['id']}'><input type='hidden' name='decision' value='Reviewed'>"
                    f"<input name='client_name' value='{esc(t['client_name'] or '', quote=True)}' placeholder='Client' size='12' title='Client'>"
                    f"<select name='chargeable'><option value='0'>Not chargeable</option><option value='1'>Chargeable to client (OPE)</option></select>"
                    f"<button class='btn btn-ok' type='submit'>Review</button></form>"
                    f"<form method='POST' action='/review_travel?ym={ym}' style='display:flex; gap:4px; margin-top:4px;'>"
                    f"<input type='hidden' name='id' value='{t['id']}'><input type='hidden' name='decision' value='Rejected'>"
                    f"<input name='note' placeholder='Reason for rejecting' size='18'><button class='btn btn-danger' type='submit'>Reject</button></form>")
        if t["emp_id"] == own_emp or role in (ROLE_APPROVER, ROLE_PREPARER):
            out += (f"<form method='POST' action='/withdraw_travel?ym={ym}' style='margin-top:4px;'><input type='hidden' name='id' value='{t['id']}'>"
                    f"<button class='btn btn-sec' type='submit'>Withdraw</button></form>")
        return out

    def mode_cells(t):
        receipt = f"<br><a href='/travel_receipt?id={t['id']}' target='_blank'>receipt</a>" if t["receipt_file"] else ""
        if (t["travel_mode"] or OWN_VEHICLE) == OWN_VEHICLE:
            return (f"<td>{OWN_VEHICLE}{receipt}</td><td class='num'>{t['kms']:g}</td><td class='num'>{t['rate_per_km']:g}</td>"
                    f"<td class='num'>—</td>")
        return (f"<td><b>{esc(t['travel_mode'])}</b><br><span class='note'>actual fare</span>{receipt}</td>"
                f"<td class='num'>{t['kms']:g}</td><td class='num'>—</td><td class='num'>{t['fare']:,.0f}</td>")

    rows_html = "".join(f"""
                <tr>
                  <td>{t['claim_date']}</td><td>{t['emp_id']}</td><td>{esc(t['employee_name'])}</td>
                  <td>{esc(t['client_name'] or '')}</td><td>{esc(t['venue_particular'] or '')}</td>
                  {mode_cells(t)}
                  <td class="num">{t['parking']:,.0f}</td><td class="num">{t['other_charges']:,.0f}</td>
                  <td class="num"><strong>{t['total_amount']:,.0f}</strong></td><td>{t['payroll_month']}</td>
                  <td>{claim_status(t)}</td><td>{claim_charge(t)}</td><td>{claim_action(t)}</td>
                </tr>""" for t in trs)
    n_to_review = sum(1 for t in trs if t["status"] == "Submitted" and not cannot_decide(t["emp_id"])) if can_review else 0

    invoice_panel = ""
    if can_review:
        outstanding = chargeable_to_invoice(conn)
        inv_rows = "".join(f"""
                    <tr><td><strong>{esc(o['client_name'])}</strong></td><td>{pay_month_label(o['payroll_month'])}</td><td class="num">{o['n']}</td>
                      <td class="num"><strong>{o['amount']:,.0f}</strong></td><td style="white-space:normal; max-width:420px;">{esc(o['detail'])}</td>
                      <td><form method="POST" action="/mark_invoiced?ym={ym}" style="display:flex; gap:4px;">
                        <input type="hidden" name="client_name" value="{esc(o['client_name'], quote=True)}">
                        <input type="hidden" name="payroll_month" value="{o['payroll_month']}">
                        <input name="invoice_no" placeholder="Invoice no." size="10">
                        <button class="btn btn-ok" type="submit">Included in Invoice</button></form></td></tr>""" for o in outstanding)
        month_charge = conn.execute("""
                    SELECT t.*, s.employee_name FROM travel_claims t JOIN staff s ON s.emp_id = t.emp_id
                    WHERE t.payroll_month = ? AND t.status = 'Reviewed' AND t.chargeable = 1 ORDER BY t.client_name, t.claim_date
                """, (pm_str,)).fetchall()
        month_rows = "".join(f"""
                    <tr><td>{esc(t['client_name'])}</td><td>{t['claim_date']}</td><td>{esc(t['employee_name'])}</td><td>{esc(t['venue_particular'] or '')}</td>
                      <td class="num">{t['total_amount']:,.0f}</td>
                      <td>{f"<span class='badge b-ok'>Invoiced {esc(t['invoice_no'] or '')}</span>" if t['invoiced'] else "<span class='badge b-warn'>Not invoiced</span>"}</td></tr>"""
            for t in month_charge)
        invoice_panel = f"""
            <div class="panel" id="chargeable">
              <h3>Chargeable Travel (Out-of-Pocket Expenses) — To Be Invoiced</h3>
              <p class="note">Chargeable claims paid in an <b>approved</b> payroll, not yet included in a client invoice. Mark each client's amount
                once it is in the invoice; until then this reminder pops up for the reviewer.</p>
              <table>
                <thead><tr><th>Client</th><th>Payroll Month</th><th class="num">Claims</th><th class="num">Amount (Rs.)</th><th>Details</th><th></th></tr></thead>
                <tbody>{inv_rows or "<tr><td colspan='6'>Nothing outstanding.</td></tr>"}</tbody>
              </table>
              <h4 style="margin:18px 0 8px;">All chargeable claims — {pay_month_label(pm_str)} payroll
                (Rs. {sum(t['total_amount'] for t in month_charge):,.0f})</h4>
              <table>
                <thead><tr><th>Client</th><th>Date</th><th>Staff</th><th>Venue</th><th class="num">Amount</th><th>Invoice</th></tr></thead>
                <tbody>{month_rows or "<tr><td colspan='6'>No chargeable claims in this month.</td></tr>"}</tbody>
              </table>
            </div>"""
    cfg_t = get_settings_dict(conn)
    base_km, base_price = float(cfg_t.get("travel_rate_base_km", 17)), float(cfg_t.get("travel_rate_base_price", 400))
    today_date, today_price = petrol_price_on(conn, date.today())
    prices = conn.execute("SELECT * FROM fuel_prices ORDER BY effective_date DESC LIMIT 15").fetchall()
    price_rows = "".join(
        f"<tr><td>{parse_date(p['effective_date']).strftime('%d %b %Y')}</td><td class='num'>Rs. {p['price']:g}</td>"
        f"<td class='num'><b>Rs. {km_rate_from_price(p['price'], cfg_t):g}</b></td><td>{p['source']}</td></tr>" for p in prices)
    max_up = float(cfg_t.get("travel_rate_max_uplift_pct", 10))
    uplift = rate_uplift_on(conn, date.today())
    formula_today = km_rate_from_price(today_price, cfg_t)
    uplifts = conn.execute("SELECT * FROM rate_uplifts ORDER BY id DESC LIMIT 10").fetchall()
    controls = ""
    if role in ROLE_ACTIONS["/set_travel_rate"]:  # approver: the formula and hand-entered prices
        controls += f"""
              <div style="display:flex; gap:24px; flex-wrap:wrap; margin:10px 0;">
                <form class="inline" method="POST" action="/set_travel_rate?ym={ym}" style="margin:0;">
                  <label>Formula: Rs. per km <input type="number" step="0.01" min="0.01" name="rate" value="{base_km:g}" required style="width:80px;"></label>
                  <label>at petrol Rs./litre <input type="number" step="0.01" min="1" name="base_price" value="{base_price:g}" required style="width:90px;"></label>
                  <button class="btn btn-sec" type="submit">Update Formula</button>
                </form>
                <form class="inline" method="POST" action="/add_fuel_price?ym={ym}" style="margin:0;">
                  <label>If the website is down — price from <input type="date" name="effective_date" required max="{date.today().isoformat()}"></label>
                  <label>Rs./litre <input type="number" step="0.01" min="1" name="price" required style="width:80px;"></label>
                  <button class="btn btn-sec" type="submit">Enter Price</button>
                </form>
              </div>"""
    if role in ROLE_ACTIONS["/set_rate_uplift"]:
        reviewer_limit = role == ROLE_REVIEWER
        controls += f"""
              <form class="inline" method="POST" action="/set_rate_uplift?ym={ym}" style="margin:10px 0;">
                <label>Increase over formula (%) <input type="number" step="0.5" min="0" {f'max="{max_up:g}"' if reviewer_limit else ''} name="pct"
                  value="{uplift['pct'] if uplift else 0:g}" required style="width:80px;"></label>
                {'' if reviewer_limit else f'<label>From <input type="date" name="effective_from" value="{date.today().isoformat()}"></label>'}
                <label>Reason (in writing) <input name="reason" required minlength="10" size="40" placeholder="e.g. heavy travel to out-of-city clients this month"></label>
                <button class="btn" type="submit">Save Increase</button>
              </form>
              <p class="note">{f"You may raise the rate by at most <b>{max_up:g}%</b> above the formula, from today, with a written reason; the approver is notified. 0% returns to the formula rate." if reviewer_limit else "As approver you may set any increase and start date."}</p>"""
    if role in ROLE_ACTIONS["/fetch_fuel_prices"]:
        controls += f"""
              <form method="POST" action="/fetch_fuel_prices?ym={ym}" style="margin:6px 0;"><button class="btn btn-sec" type="submit">Fetch Latest PSO Prices Now</button></form>
              <p class="note">Last automatic check: {html_lib.escape(cfg_t.get('fuel_price_last_check', 'not yet'))} (checked every 6 hours).
                Every formula change, increase or hand-entered price is recorded in the Approval Log.</p>"""
    uplift_rows = "".join(
        f"<tr><td>{parse_date(u['effective_from']).strftime('%d %b %Y')}</td><td class='num'>{u['pct']:g}%</td>"
        f"<td style='white-space:normal;'>{html_lib.escape(u['reason'])}</td><td>{html_lib.escape(u['set_by'] or '')}</td></tr>" for u in uplifts)
    rate_panel = f"""
            <div class="panel">
              <h3>Travelling Rate per Km — linked to the PSO petrol price</h3>
              <p class="note">The rate follows the <b>PSO Premier Euro 5</b> petrol price
                (<a href="{html_lib.escape(cfg_t.get('fuel_price_url', ''))}" target="_blank" rel="noopener">psopk.com</a>), read automatically:
                <b>rate per km = petrol price × {base_km:g} ÷ {base_price:g}</b> (Rs. {base_km:g}/km when petrol is Rs. {base_price:g}/litre).
                Each claim uses the price in force on the <b>date travelled</b>.</p>
              <div class="cards" style="margin-bottom:10px;">
                <div class="card"><h3>Petrol Today (Premier Euro 5)</h3><div class="val">{f"Rs. {today_price:g}" if today_price else "—"}</div>
                  <div class="sub">{f"effective {parse_date(today_date).strftime('%d %b %Y')}" if today_date else "no price on file yet"}</div></div>
                <div class="card"><h3>Formula Rate</h3><div class="val">Rs. {formula_today:g} / km</div>
                  <div class="sub">{f"{today_price:g} × {base_km:g} ÷ {base_price:g}" if today_price else "fallback rate (no petrol price yet)"}</div></div>
                <div class="card"><h3>Increase</h3><div class="val">{f"{uplift['pct']:g}%" if uplift and uplift['pct'] else "None"}</div>
                  <div class="sub">{html_lib.escape(f"{uplift['reason']} — {uplift['set_by']}") if uplift and uplift['pct'] else "rate = formula"}</div></div>
                <div class="card" style="border-left-color:#16a34a;"><h3>Travel Rate Today</h3><div class="val">Rs. {km_rate:g} / km</div>
                  <div class="sub">used for claims dated today</div></div>
              </div>
              {controls}
              <details><summary style="cursor:pointer;">Recent prices and rates</summary>
                <table style="margin-top:8px;"><thead><tr><th>Effective From</th><th class="num">Petrol Rs./Litre</th><th class="num">Formula Rs./Km</th><th>Source</th></tr></thead>
                  <tbody>{price_rows or "<tr><td colspan='4'>No prices yet.</td></tr>"}</tbody></table>
                {f'<h4 style="margin:14px 0 6px;">Increases over the formula</h4><table><thead><tr><th>From</th><th class="num">Increase</th><th>Reason</th><th>By</th></tr></thead><tbody>{uplift_rows}</tbody></table>' if uplifts else ''}</details>
            </div>"""
    body = rate_panel + f"""
            <div class="panel">
              <h3>Submit Travel Claim (25th cut-off rule)</h3>
              <p class="note">{travel_note}<br>
                <b>Own vehicle</b>: kms × the rate on the date travelled. <b>InDrive, Yango, cab, auto rickshaw or bike ride service</b>:
                the <b>actual fare</b> is reimbursed once the reviewer approves it — attach the receipt or app screenshot if you have it.
                Parking and other charges can be added to either.</p>
              <form class="inline" method="POST" action="/add_travel?ym={ym}" enctype="multipart/form-data">
                <label>Date <input type="date" name="claim_date" required max="{date.today().isoformat()}"></label>
                {travel_who}
                <label>Client <input name="client_name" placeholder="Client / IPAC / FBR"></label>
                <label>Venue <input name="venue_particular" placeholder="Visit details"></label>
                <label>Travelled By <select name="travel_mode">{"".join(f"<option>{m}</option>" for m in TRAVEL_MODES)}</select></label>
                <label>Kms (own vehicle) <input type="number" step="0.1" min="0" name="kms" value="0" style="width:75px;"></label>
                <label>Actual Fare (ride service) <input type="number" min="0" name="fare" value="0" style="width:90px;"></label>
                <label>Receipt / Screenshot <input type="file" name="receipt" accept=".pdf,.jpg,.jpeg,.png"></label>
                <label>Parking <input type="number" min="0" name="parking" value="0" style="width:75px;"></label>
                <label>Other <input type="number" min="0" name="other_charges" value="0" style="width:75px;"></label>
                <button class="btn" type="submit">Submit Claim</button>
              </form>
            </div>
            <div class="panel">
              <h3>Travel Claims{f" — <span class='badge b-warn'>{n_to_review} waiting for your review</span>" if n_to_review else ""}</h3>
              <table>
                <thead><tr><th>Date</th><th>ID</th><th>Name</th><th>Client</th><th>Venue</th><th>Travelled By</th><th class="num">Kms</th><th class="num">Rate</th><th class="num">Fare</th><th class="num">Parking</th><th class="num">Other</th><th class="num">Total</th><th>Payroll Month</th><th>Status</th><th>Chargeable</th><th></th></tr></thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>""" + invoice_panel
    return body
