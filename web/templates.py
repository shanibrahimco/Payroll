"""Page templates for the web app."""

import html as html_lib

import config
from common.constants import MIN_PASSWORD_LEN

AUTH_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — {firm_short} Payroll</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; background: #0f172a; color: #1e293b; min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
  .box {{ background: #fff; border-radius: 10px; padding: 30px 32px; width: 100%; max-width: 360px; margin: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.3); }}
  h1 {{ margin: 0 0 4px 0; font-size: 19px; font-weight: 600; color: #0f172a; }}
  .sub {{ margin: 0 0 20px 0; font-size: 13px; color: #64748b; }}
  form {{ display: flex; flex-direction: column; gap: 14px; }}
  label {{ display: flex; flex-direction: column; font-size: 12px; font-weight: 600; color: #475569; gap: 5px; }}
  input {{ padding: 9px 11px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 14px; }}
  input:focus {{ outline: 2px solid #38bdf8; border-color: #38bdf8; }}
  .btn {{ background: #0284c7; color: #fff; border: none; padding: 10px 14px; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500; margin-top: 4px; }}
  .btn:hover {{ background: #0369a1; }}
  .error {{ background: #fee2e2; color: #991b1b; padding: 9px 12px; border-radius: 6px; font-size: 13px; margin-bottom: 14px; }}
</style>
</head>
<body>
<div class="box">
  <h1>{firm_short} Payroll</h1>
  <p class="sub">{subtitle}</p>
  {error}
  <form method="POST" action="{action}">
    <label>Username <input name="username" value="{username}" required autofocus autocomplete="username"></label>
    <label>Password <input type="password" name="password" required autocomplete="{pw_autocomplete}"></label>
    {confirm}
    <button class="btn" type="submit">{button}</button>
  </form>
</div>
</body>
</html>
"""

def render_auth_page(setup, error="", username=""):
    return AUTH_TEMPLATE.format(
        firm_short=html_lib.escape(config.FIRM_SHORT),
        title="Create Admin Account" if setup else "Sign In",
        subtitle=(f"First-time setup: create the administrator account (password at least {MIN_PASSWORD_LEN} characters)."
                  if setup else "Sign in to continue."),
        error=f'<div class="error">{html_lib.escape(error)}</div>' if error else "",
        action="/setup" if setup else "/login",
        username=html_lib.escape(username),
        pw_autocomplete="new-password" if setup else "current-password",
        confirm=('<label>Confirm Password <input type="password" name="confirm" required autocomplete="new-password"></label>'
                 if setup else ""),
        button="Create Account" if setup else "Sign In",
    )

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{firm_short} — Payroll System</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; background: #f4f6f9; color: #1e293b; }}
  header {{ background: #0f172a; color: #fff; padding: 16px 28px; display: flex; justify-content: space-between; align-items: center; }}
  header h1 {{ margin: 0; font-size: 19px; font-weight: 600; }}
  header span {{ font-size: 12.5px; color: #94a3b8; }}
  nav {{ background: #1e293b; padding: 0 28px; display: flex; gap: 6px; }}
  nav a {{ color: #cbd5e1; text-decoration: none; padding: 11px 16px; font-size: 13px; font-weight: 500; border-bottom: 3px solid transparent; }}
  nav a.active, nav a:hover {{ color: #fff; border-bottom-color: #38bdf8; background: rgba(255,255,255,0.05); }}
  .container {{ max-width: 1400px; margin: 22px auto; padding: 0 24px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin-bottom: 22px; }}
  .card {{ background: #fff; border-radius: 8px; padding: 16px 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.07); border-left: 4px solid #0284c7; }}
  .card h3 {{ margin: 0 0 6px 0; font-size: 12px; text-transform: uppercase; color: #64748b; letter-spacing: 0.4px; }}
  .card .val {{ font-size: 22px; font-weight: 700; color: #0f172a; }}
  .card .sub {{ font-size: 12px; color: #64748b; margin-top: 4px; }}
  .panel {{ background: #fff; border-radius: 8px; padding: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.07); margin-bottom: 20px; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid #e2e8f0; text-align: left; white-space: nowrap; }}
  th {{ background: #f8fafc; font-weight: 600; color: #334155; }}
  tr:hover {{ background: #f8fafc; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .badge {{ padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
  .b-warn {{ background: #fef3c7; color: #92400e; }}
  .b-ok {{ background: #dcfce7; color: #166534; }}
  .btn {{ background: #0284c7; color: #fff; border: none; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 500; text-decoration: none; display: inline-block; }}
  .btn:hover {{ background: #0369a1; }}
  .btn-sec {{ background: #475569; }}
  form.inline {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 14px; }}
  label {{ display: flex; flex-direction: column; font-size: 12px; font-weight: 600; color: #475569; gap: 4px; }}
  input, select {{ padding: 7px 10px; border: 1px solid #cbd5e1; border-radius: 5px; font-size: 13px; }}
  .flash {{ padding: 11px 16px; border-radius: 6px; font-size: 13.5px; margin-bottom: 18px; }}
  .flash-ok {{ background: #dcfce7; color: #166534; }}
  .flash-warn {{ background: #fef3c7; color: #92400e; }}
  .runbar {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .btn-ok {{ background: #16a34a; }}
  .btn-ok:hover {{ background: #15803d; }}
  .btn-danger {{ background: #dc2626; }}
  .btn-danger:hover {{ background: #b91c1c; }}
  .note {{ font-size: 12.5px; color: #64748b; margin: 0 0 12px 0; }}
  .modal-bg {{ position: fixed; inset: 0; background: rgba(15,23,42,0.55); display: flex; align-items: center; justify-content: center; z-index: 50; padding: 16px; }}
  .modal {{ background: #fff; border-radius: 10px; padding: 22px 24px; max-width: 640px; width: 100%; max-height: 85vh; overflow: auto; box-shadow: 0 10px 30px rgba(0,0,0,0.3); }}
</style>
</head>
<body>
<header>
  <div>
    <h1>{firm_name}</h1>
    <span>Tax Deduction: Rs. 0 (Disabled) &nbsp;|&nbsp; EOBI: 5% Er + 1% Ee Both Borne by Employer &nbsp;|&nbsp; Travel Rate: Rs. {km_rate}/km</span>
  </div>
  <div style="display:flex; gap:18px; align-items:center;">
    <form method="GET" action="/" style="display:flex; gap:8px; align-items:center;">
      <input type="hidden" name="tab" value="{tab}">
      <label style="color:#cbd5e1; flex-direction:row; align-items:center;">Month:
        <input type="month" name="ym" value="{ym}" onchange="this.form.submit()">
      </label>
      {header_actions}
    </form>
    <form method="POST" action="/logout" style="display:flex; gap:8px; align-items:center;">
      <span>Signed in as <b style="color:#fff;">{user}</b> ({role})</span>
      <button class="btn btn-sec" type="submit">Log out</button>
    </form>
  </div>
</header>
<nav>
  {nav_links}
</nav>
<div class="container">
  {flash}
  {body}
</div>
{popup}
</body>
</html>
"""
