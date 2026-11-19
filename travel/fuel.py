"""Petrol prices and the per-km travel rate."""

from datetime import date
from datetime import datetime

from common.dates import fmt_date
from common.settings import get_settings_dict
from database import get_conn

FUEL_PRODUCT = "PREMIER EURO 5"

def parse_pso_prices(html):
    """[(YYYY-MM-DD, price)] for Premier Euro 5 from a PSO 'POL archives' page: blocks headed
    'Effective From: November 18, 2026', each with a 'PREMIER EURO 5 | Rs.325/Ltr' row."""
    import re
    out = []
    blocks = re.split(r"Effective From:\s*", html)[1:]
    for block in blocks:
        m_date = re.match(r"([A-Za-z]+ \d{1,2}, \d{4})", block)
        m_price = re.search(re.escape(FUEL_PRODUCT) + r"\s*</td>\s*<td>\s*Rs\.?\s*([\d,]+(?:\.\d+)?)\s*/\s*Ltr", block, re.I)
        if not m_date or not m_price:
            continue
        try:
            d = datetime.strptime(m_date.group(1), "%B %d, %Y").date()
        except ValueError:
            continue
        price = float(m_price.group(1).replace(",", ""))
        if price > 0:
            out.append((fmt_date(d), price))
    return out

def fetch_fuel_prices(conn, pages=1, url=None):
    """Read the latest Premier Euro 5 prices from the PSO website into fuel_prices (manual entries are kept).
    Returns the number of new dates added; raises on a network / format problem."""
    import urllib.request
    base = url or get_settings_dict(conn).get("fuel_price_url", "https://psopk.com/fuel-prices/pol/archives?page=1")
    added = found = 0
    now = datetime.now().isoformat(timespec="seconds")
    for page in range(1, pages + 1):
        page_url = base.split("?")[0] + f"?page={page}"
        req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0 (payroll travel-rate update)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", "replace")
        rows = parse_pso_prices(html)
        found += len(rows)
        for d, price in rows:
            cur = conn.execute("INSERT OR IGNORE INTO fuel_prices (effective_date, price, source, recorded_by, recorded_at) VALUES (?,?,?,?,?)",
                               (d, price, "PSO website", "system", now))
            added += cur.rowcount
    if not found:
        raise ValueError(f"No '{FUEL_PRODUCT}' prices found on the PSO page (the page layout may have changed)")
    return added

def petrol_price_on(conn, on_date):
    """(effective date, price) of the Premier Euro 5 price in force on a date, or (None, None)."""
    row = conn.execute("SELECT effective_date, price FROM fuel_prices WHERE effective_date <= ? ORDER BY effective_date DESC LIMIT 1",
                       (fmt_date(on_date) if isinstance(on_date, date) else on_date,)).fetchone()
    return (row["effective_date"], row["price"]) if row else (None, None)

def travel_rate_on(conn, on_date, cfg=None):
    """Rs. per km for a claim dated `on_date`: petrol price that day x base Rs./km ÷ base petrol price
    (Rs. 17/km at Rs. 400/litre). Falls back to travel_rate_per_km when no price is on file."""
    cfg = cfg or get_settings_dict(conn)
    _, price = petrol_price_on(conn, on_date)
    uplift = rate_uplift_on(conn, on_date)
    return km_rate_from_price(price, cfg, uplift["pct"] if uplift else 0)

def rate_uplift_on(conn, on_date):
    """The uplift in force on a date (row with pct, reason, set_by), or None."""
    return conn.execute("SELECT * FROM rate_uplifts WHERE effective_from <= ? ORDER BY effective_from DESC, id DESC LIMIT 1",
                        (fmt_date(on_date) if isinstance(on_date, date) else on_date,)).fetchone()

def km_rate_from_price(price, cfg, uplift_pct=0):
    """price x base Rs./km ÷ base price (or the fallback rate when no price), plus any uplift %, to the paisa,
    rounded half up (exact decimal arithmetic)."""
    from decimal import Decimal, ROUND_HALF_UP
    if price is None:
        rate = Decimal(str(cfg.get("travel_rate_per_km", 17)))
    else:
        rate = (Decimal(str(price)) * Decimal(str(cfg.get("travel_rate_base_km", 17)))
                / Decimal(str(cfg.get("travel_rate_base_price", 400))))
    rate *= 1 + Decimal(str(uplift_pct or 0)) / 100
    return float(rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def fuel_price_updater(interval_hours=6):
    """Background thread: keep the PSO petrol prices current (first run also fills the last few months)."""
    import threading, time
    def run():
        while True:
            conn = get_conn()
            try:
                empty = conn.execute("SELECT COUNT(*) FROM fuel_prices WHERE source = 'PSO website'").fetchone()[0] == 0
                added = fetch_fuel_prices(conn, pages=6 if empty else 1)
                status = f"OK — {added} new price(s)"
            except Exception as e:  # offline or the site changed: keep the last prices, show the problem on the Travel tab
                status = f"Failed — {type(e).__name__}: {str(e)[:150]}"
            conn.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('fuel_price_last_check', ?, 'Last PSO price check')",
                         (f"{datetime.now().isoformat(timespec='minutes')} {status}",))
            conn.commit()
            conn.close()
            time.sleep(interval_hours * 3600)
    threading.Thread(target=run, daemon=True).start()
