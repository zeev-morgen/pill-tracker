"""
Web dashboard — served at http://localhost:8080/
Shows live stock prices, recent alerts, and system status.
"""

from datetime import datetime, timezone
from typing import Dict, List

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from .store import alert_store

router = APIRouter()

# ── Shared live-price cache (written by monitor_cycle, read by dashboard) ────
_price_cache: Dict[str, dict] = {}


def update_price_cache(data: dict) -> None:
    _price_cache[data["symbol"]] = {
        "symbol":          data["symbol"],
        "price":           data["price"],
        "change_pct":      data["change_pct"],
        "from_open_pct":   data.get("from_open_pct"),
        "since_close_pct": data.get("since_close_pct"),
        "volume":          data["volume"],
        "day_high":        data.get("day_high"),
        "day_low":         data.get("day_low"),
        "session":         data["session"],
        "updated":         datetime.now().strftime("%H:%M:%S"),
    }


# ── API ───────────────────────────────────────────────────────────────────────

@router.get("/api/status")
async def api_status():
    return JSONResponse({
        "stocks": list(_price_cache.values()),
        "alerts": [
            {
                "timestamp":  a.timestamp,
                "symbol":     a.symbol,
                "alert_type": a.alert_type,
                "message":    a.message,
                "severity":   a.severity,
            }
            for a in alert_store.recent(50)
        ],
        "server_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    })


# ── Dashboard HTML ────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=_HTML)


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Monitor</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e;
    --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; }
  header {
    background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 16px 24px; display: flex; align-items: center; gap: 12px;
  }
  header h1 { font-size: 1.2rem; font-weight: 600; }
  #session-badge {
    padding: 3px 10px; border-radius: 20px; font-size: 0.75rem;
    font-weight: 600; background: #1f6feb33; color: var(--blue); border: 1px solid #1f6feb;
  }
  #server-time { margin-left: auto; color: var(--muted); font-size: 0.8rem; }
  #refresh-indicator { width: 8px; height: 8px; border-radius: 50%; background: var(--green); }
  main { padding: 24px; display: grid; gap: 24px; max-width: 1200px; margin: 0 auto; }

  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .card-title { padding: 14px 18px; font-size: 0.85rem; font-weight: 600;
    color: var(--muted); border-bottom: 1px solid var(--border); text-transform: uppercase; letter-spacing: .05em; }

  table { width: 100%; border-collapse: collapse; }
  th { padding: 10px 18px; text-align: left; font-size: 0.75rem; color: var(--muted);
    font-weight: 500; border-bottom: 1px solid var(--border); }
  td { padding: 12px 18px; font-size: 0.9rem; border-bottom: 1px solid #21262d; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #ffffff08; }

  .symbol { font-weight: 700; color: var(--blue); }
  .price  { font-weight: 600; font-variant-numeric: tabular-nums; }
  .up     { color: var(--green); }
  .down   { color: var(--red); }
  .flat   { color: var(--muted); }
  .volume { color: var(--muted); font-size: 0.82rem; }
  .session-tag {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 0.72rem; font-weight: 600;
  }
  .session-regular { background:#3fb95022; color: var(--green); }
  .session-pre     { background:#d2992222; color: var(--yellow); }
  .session-after   { background:#58a6ff22; color: var(--blue); }
  .session-closed  { background:#30363d;   color: var(--muted); }

  .alert-row td { font-size: 0.85rem; }
  .sev-INFO     { color: var(--blue); }
  .sev-WARNING  { color: var(--yellow); }
  .sev-CRITICAL { color: var(--red); }
  .alert-msg { color: var(--text); }
  .alert-ts  { color: var(--muted); font-size: 0.78rem; white-space: nowrap; }

  #no-stocks, #no-alerts { padding: 32px; text-align: center; color: var(--muted); font-size: 0.9rem; }

  .progress-bar {
    height: 2px; background: var(--border); position: fixed; top: 0; left: 0; width: 100%; z-index: 999;
  }
  #progress { height: 100%; background: var(--blue); width: 0%; transition: width linear; }
</style>
</head>
<body>
<div class="progress-bar"><div id="progress"></div></div>

<header>
  <div id="refresh-indicator"></div>
  <h1>📈 Stock Monitor</h1>
  <span id="session-badge">—</span>
  <span id="server-time">Loading…</span>
</header>

<main>
  <div class="card">
    <div class="card-title">Live Prices</div>
    <div id="stocks-wrap">
      <div id="no-stocks">Waiting for first data poll (up to 60 seconds)…</div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Recent Alerts</div>
    <div id="alerts-wrap">
      <div id="no-alerts">No alerts yet.</div>
    </div>
  </div>
</main>

<script>
const INTERVAL = 60000;
let nextRefresh = Date.now() + INTERVAL;

function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(1)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(1)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(0)+'K';
  return n.toString();
}

function sessionClass(s) { return 'session-'+s; }
function sessionLabel(s) {
  return {pre:'Pre-Market', regular:'Regular', after:'After-Hours', closed:'Closed'}[s] || s;
}

function pctCell(val) {
  if (val == null) return '<span class="flat">—</span>';
  const cls  = val > 0 ? 'up' : val < 0 ? 'down' : 'flat';
  const sign = val >= 0 ? '+' : '';
  return `<span class="${cls}">${sign}${val.toFixed(2)}%</span>`;
}

function renderStocks(stocks) {
  if (!stocks.length) return;
  const html = `<table>
    <thead><tr>
      <th>סמל</th><th>מחיר</th>
      <th>שינוי יומי</th>
      <th>מסגירה</th>
      <th>נפח</th><th>גבוה / נמוך</th><th>סשן</th><th>עודכן</th>
    </tr></thead>
    <tbody>${stocks.map(s => {
      const scVal = s.since_close_pct != null ? s.since_close_pct : s.from_open_pct;
      const hi = s.day_high ? '$'+s.day_high.toFixed(2) : '—';
      const lo = s.day_low  ? '$'+s.day_low.toFixed(2)  : '—';
      return `<tr>
        <td><span class="symbol">${s.symbol}</span></td>
        <td><span class="price">$${s.price.toFixed(2)}</span></td>
        <td>${pctCell(s.change_pct)}</td>
        <td>${pctCell(scVal)}</td>
        <td><span class="volume">${fmt(s.volume)}</span></td>
        <td><span class="volume">${hi} / ${lo}</span></td>
        <td><span class="session-tag ${sessionClass(s.session)}">${sessionLabel(s.session)}</span></td>
        <td><span class="volume">${s.updated}</span></td>
      </tr>`;
    }).join('')}</tbody>
  </table>`;
  document.getElementById('stocks-wrap').innerHTML = html;
}

function renderAlerts(alerts) {
  if (!alerts.length) { document.getElementById('alerts-wrap').innerHTML = '<div id="no-alerts">No alerts yet.</div>'; return; }
  const html = `<table>
    <thead><tr><th>Time</th><th>Symbol</th><th>Type</th><th>Message</th></tr></thead>
    <tbody>${alerts.map(a => `<tr class="alert-row">
      <td class="alert-ts">${a.timestamp}</td>
      <td><span class="symbol">${a.symbol}</span></td>
      <td><span class="sev-${a.severity}">${a.severity}</span></td>
      <td class="alert-msg">${a.message}</td>
    </tr>`).join('')}</tbody>
  </table>`;
  document.getElementById('alerts-wrap').innerHTML = html;
}

async function refresh() {
  document.getElementById('refresh-indicator').style.background = '#d29922';
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    renderStocks(data.stocks);
    renderAlerts(data.alerts);
    document.getElementById('server-time').textContent = data.server_time;
    const session = data.stocks[0]?.session || 'closed';
    const badge = document.getElementById('session-badge');
    badge.textContent = sessionLabel(session);
    badge.className = '';
    badge.style.cssText = '';
    const colors = {regular:'#3fb950',pre:'#d29922',after:'#58a6ff',closed:'#8b949e'};
    badge.style.cssText = `padding:3px 10px;border-radius:20px;font-size:.75rem;font-weight:600;
      background:${colors[session]}22;color:${colors[session]};border:1px solid ${colors[session]}`;
    document.getElementById('refresh-indicator').style.background = '#3fb950';
  } catch(e) {
    document.getElementById('refresh-indicator').style.background = '#f85149';
  }
  nextRefresh = Date.now() + INTERVAL;
}

function tickProgress() {
  const pct = Math.max(0, ((INTERVAL - (nextRefresh - Date.now())) / INTERVAL) * 100);
  document.getElementById('progress').style.width = pct + '%';
  requestAnimationFrame(tickProgress);
}

refresh();
setInterval(refresh, INTERVAL);
tickProgress();
</script>
</body>
</html>
"""
