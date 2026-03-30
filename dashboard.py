"""
dashboard.py
------------
Lightweight web dashboard for monitoring the Polymarket trading bot.

Runs a Flask server on port 8080 that reads bot state from shared JSON
files written by the main bot loop. No direct coupling to the bot process.

Endpoints:
  GET /          — Main dashboard (HTML, auto-refreshes every 15s)
  GET /api/state — Raw JSON state for programmatic access

Usage:
  Started automatically by main.py in a background thread.
  Access at http://<server-ip>:8080

State file: dashboard_state.json (written by main.py each cycle)
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("bot.dashboard")

STATE_FILE = "dashboard_state.json"
DASHBOARD_PORT = 8080

# ── State writer (called from main.py) ───────────────────────────────────────

def write_dashboard_state(
    cycle: int,
    positions: list,
    realised_pnl: float,
    unrealised_pnl: float,
    daily_pnl: float,
    total_trades: int,
    open_orders: int,
    strategies_active: list,
    signals_this_cycle: int,
    executed_this_cycle: int,
    filtered_this_cycle: int,
    kill_switch: bool,
    wallet_count: int,
    cycle_time: float,
    mode: str,
) -> None:
    """Write current bot state to a JSON file for the dashboard to read."""
    state = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_ts": time.time(),
        "cycle": cycle,
        "mode": mode,
        "kill_switch": kill_switch,
        "pnl": {
            "realised": round(realised_pnl, 2),
            "unrealised": round(unrealised_pnl, 2),
            "total": round(realised_pnl + unrealised_pnl, 2),
            "daily": round(daily_pnl, 2),
        },
        "positions": [],
        "stats": {
            "total_trades": total_trades,
            "open_orders": open_orders,
            "signals_this_cycle": signals_this_cycle,
            "executed_this_cycle": executed_this_cycle,
            "filtered_this_cycle": filtered_this_cycle,
            "wallet_count": wallet_count,
            "cycle_time_sec": round(cycle_time, 1),
            "strategies_active": strategies_active,
        },
    }

    for pos in positions:
        entry = getattr(pos, "entry_price", 0)
        current = getattr(pos, "current_price", 0)
        size = getattr(pos, "size", 0)
        pnl = (current - entry) * size if current > 0 and entry > 0 else 0
        pnl_pct = ((current / entry) - 1) * 100 if entry > 0 and current > 0 else 0

        state["positions"].append({
            "outcome": getattr(pos, "outcome", "")[:40],
            "market_id": getattr(pos, "market_id", "")[:20],
            "entry_price": round(entry, 4),
            "current_price": round(current, 4),
            "size": round(size, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 1),
            "age_hours": round((time.time() - getattr(pos, "opened_at", time.time())) / 3600, 1),
        })

    # Sort positions by P&L descending
    state["positions"].sort(key=lambda p: p["pnl"], reverse=True)

    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        logger.debug("Failed to write dashboard state: %s", exc)


# ── Dashboard HTML ───────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="15">
<title>Polymarket Bot Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
         background: #0a0a0f; color: #e0e0e0; padding: 20px; }
  .header { text-align: center; margin-bottom: 24px; }
  .header h1 { font-size: 1.4em; color: #7c8aff; }
  .header .mode { font-size: 0.85em; color: #888; margin-top: 4px; }
  .header .updated { font-size: 0.75em; color: #555; margin-top: 2px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .card { background: #14141f; border: 1px solid #222; border-radius: 8px; padding: 16px; text-align: center; }
  .card .label { font-size: 0.7em; color: #888; text-transform: uppercase; letter-spacing: 1px; }
  .card .value { font-size: 1.6em; font-weight: bold; margin-top: 4px; }
  .positive { color: #4caf50; }
  .negative { color: #f44336; }
  .neutral { color: #e0e0e0; }
  .warning { color: #ff9800; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 24px; }
  th { background: #14141f; color: #7c8aff; font-size: 0.75em; text-transform: uppercase;
       letter-spacing: 1px; padding: 10px 8px; text-align: left; border-bottom: 1px solid #222; }
  td { padding: 8px; border-bottom: 1px solid #1a1a2a; font-size: 0.85em; }
  tr:hover { background: #1a1a2a; }
  .section-title { font-size: 0.9em; color: #7c8aff; margin-bottom: 8px; text-transform: uppercase;
                   letter-spacing: 1px; }
  .strategies { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 24px; }
  .strat-tag { background: #1a1a2a; border: 1px solid #333; border-radius: 4px; padding: 4px 10px;
               font-size: 0.75em; color: #aaa; }
  .kill-switch { background: #f44336; color: white; padding: 12px; border-radius: 8px;
                 text-align: center; font-weight: bold; margin-bottom: 20px; }
  .no-positions { color: #555; text-align: center; padding: 30px; font-style: italic; }
</style>
</head>
<body>

<div class="header">
  <h1>Polymarket Trading Bot</h1>
  <div class="mode">{{ mode }} mode | Cycle #{{ cycle }} | {{ cycle_time }}s/cycle</div>
  <div class="updated">Updated: {{ updated_at }}</div>
</div>

{% if kill_switch %}
<div class="kill-switch">KILL SWITCH ACTIVE — ALL TRADING HALTED</div>
{% endif %}

<div class="grid">
  <div class="card">
    <div class="label">Total P&L</div>
    <div class="value {{ 'positive' if total_pnl >= 0 else 'negative' }}">${{ "%.2f"|format(total_pnl) }}</div>
  </div>
  <div class="card">
    <div class="label">Realised</div>
    <div class="value {{ 'positive' if realised >= 0 else 'negative' }}">${{ "%.2f"|format(realised) }}</div>
  </div>
  <div class="card">
    <div class="label">Unrealised</div>
    <div class="value {{ 'positive' if unrealised >= 0 else 'negative' }}">${{ "%.2f"|format(unrealised) }}</div>
  </div>
  <div class="card">
    <div class="label">Daily P&L</div>
    <div class="value {{ 'positive' if daily >= 0 else 'negative' }}">${{ "%.2f"|format(daily) }}</div>
  </div>
  <div class="card">
    <div class="label">Open Positions</div>
    <div class="value neutral">{{ positions|length }}</div>
  </div>
  <div class="card">
    <div class="label">Total Trades</div>
    <div class="value neutral">{{ total_trades }}</div>
  </div>
  <div class="card">
    <div class="label">Signals / Executed</div>
    <div class="value neutral">{{ signals }} / {{ executed }}</div>
  </div>
  <div class="card">
    <div class="label">Wallets Tracked</div>
    <div class="value neutral">{{ wallet_count }}</div>
  </div>
</div>

<div class="section-title">Active Strategies ({{ strategies|length }})</div>
<div class="strategies">
  {% for s in strategies %}
  <span class="strat-tag">{{ s }}</span>
  {% endfor %}
</div>

<div class="section-title">Open Positions</div>
{% if positions %}
<table>
  <thead>
    <tr>
      <th>Outcome</th>
      <th>Entry</th>
      <th>Current</th>
      <th>Size</th>
      <th>P&L</th>
      <th>P&L %</th>
      <th>Age</th>
    </tr>
  </thead>
  <tbody>
    {% for p in positions %}
    <tr>
      <td>{{ p.outcome }}</td>
      <td>${{ "%.3f"|format(p.entry_price) }}</td>
      <td>${{ "%.3f"|format(p.current_price) }}</td>
      <td>{{ "%.1f"|format(p.size) }}</td>
      <td class="{{ 'positive' if p.pnl >= 0 else 'negative' }}">${{ "%.2f"|format(p.pnl) }}</td>
      <td class="{{ 'positive' if p.pnl_pct >= 0 else 'negative' }}">{{ "%.1f"|format(p.pnl_pct) }}%</td>
      <td>{{ "%.1f"|format(p.age_hours) }}h</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<div class="no-positions">No open positions</div>
{% endif %}

</body>
</html>"""


# ── Flask server ─────────────────────────────────────────────────────────────

def _run_dashboard_server() -> None:
    """Start the Flask dashboard server (runs in a background thread)."""
    try:
        from flask import Flask, render_template_string, jsonify
    except ImportError:
        logger.warning(
            "Flask not installed — dashboard disabled. "
            "Install with: pip install flask"
        )
        return

    import os
    os.environ["FLASK_SKIP_DOTENV"] = "1"

    app = Flask(__name__)
    app.logger.setLevel(logging.WARNING)

    wlog = logging.getLogger("werkzeug")
    wlog.setLevel(logging.WARNING)

    @app.route("/")
    def index():
        state = _read_state()
        return render_template_string(
            DASHBOARD_HTML,
            mode=state.get("mode", "?"),
            cycle=state.get("cycle", 0),
            cycle_time=state.get("stats", {}).get("cycle_time_sec", 0),
            updated_at=state.get("updated_at", "never"),
            kill_switch=state.get("kill_switch", False),
            total_pnl=state.get("pnl", {}).get("total", 0),
            realised=state.get("pnl", {}).get("realised", 0),
            unrealised=state.get("pnl", {}).get("unrealised", 0),
            daily=state.get("pnl", {}).get("daily", 0),
            positions=state.get("positions", []),
            total_trades=state.get("stats", {}).get("total_trades", 0),
            signals=state.get("stats", {}).get("signals_this_cycle", 0),
            executed=state.get("stats", {}).get("executed_this_cycle", 0),
            wallet_count=state.get("stats", {}).get("wallet_count", 0),
            strategies=state.get("stats", {}).get("strategies_active", []),
        )

    @app.route("/api/state")
    def api_state():
        return jsonify(_read_state())

    try:
        logger.info("Dashboard starting on port %d", DASHBOARD_PORT)
        app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)
    except Exception as exc:
        logger.error("Dashboard server crashed: %s", exc, exc_info=True)


def _read_state() -> dict:
    """Read the latest state from the JSON file."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"updated_at": "no data yet", "pnl": {}, "positions": [], "stats": {}}


def start_dashboard_thread() -> None:
    """Launch the dashboard in a daemon thread (non-blocking)."""
    t = threading.Thread(target=_run_dashboard_server, daemon=True, name="dashboard")
    t.start()
    logger.info("Dashboard thread started on port %d", DASHBOARD_PORT)
