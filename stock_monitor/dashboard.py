"""
Web dashboard — served at http://localhost:8080/

Shows live stock prices, recent alerts, the personal portfolio (manually
entered positions with entry price), ATR volatility and sector-concentration
risk tabs, and allocation pie charts.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List

from fastapi import APIRouter, Body, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

from .portfolio_risk import PortfolioRiskAnalyzer
from .store import Holding, HoldingError, alert_store, portfolio_store

logger = logging.getLogger(__name__)

router = APIRouter()

_risk_analyzer = PortfolioRiskAnalyzer(portfolio_store)

# The AI analyst is built by main.py only when a key is configured, and the
# data feed is owned by the running app, so both reach the dashboard through
# this registry rather than being constructed a second time here.
_analyst = None
_data_feed = None


def set_analyst(analyst) -> None:
    global _analyst
    _analyst = analyst


def get_analyst():
    return _analyst


def set_data_feed(feed) -> None:
    global _data_feed
    _data_feed = feed


def get_data_feed():
    """The app's feed when running as a daemon; a standalone one otherwise."""
    global _data_feed
    if _data_feed is None:
        from .data_feed import StockDataFeed

        _data_feed = StockDataFeed()
    return _data_feed

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


# ── Portfolio API ─────────────────────────────────────────────────────────────

@router.get("/api/holdings")
async def api_list_holdings():
    return JSONResponse([h.as_dict() for h in portfolio_store.all()])


@router.post("/api/holdings")
async def api_upsert_holding(payload: dict = Body(...)):
    """Create or update a position. Saving an existing ticker edits it."""
    try:
        holding = Holding.create(
            ticker=payload.get("ticker", ""),
            quantity=payload.get("quantity"),
            entry_price=payload.get("entry_price"),
            sector=payload.get("sector"),
            asset_type=payload.get("asset_type"),
        )
    except HoldingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    portfolio_store.upsert(holding)
    return JSONResponse(holding.as_dict(), status_code=201)


@router.delete("/api/holdings/{ticker}")
async def api_delete_holding(ticker: str):
    if not portfolio_store.delete(ticker):
        raise HTTPException(status_code=404, detail="הפוזיציה לא נמצאה")
    return JSONResponse({"status": "deleted", "ticker": ticker.strip().upper()})


@router.post("/api/analyze/{ticker}")
async def api_analyze_ticker(ticker: str):
    """AI analysis of a single stock, personalized to the held position.

    When the ticker is in the portfolio the recommendation is tailored to the
    user's entry price; otherwise it is a general assessment.
    """
    analyst = get_analyst()
    if analyst is None:
        raise HTTPException(
            status_code=503,
            detail="ניתוח AI אינו מופעל — הגדר ANTHROPIC_API_KEY ו-ai.enabled: true",
        )
    try:
        symbol = Holding.create(ticker, 1, 1).ticker   # reuse ticker validation
    except HoldingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    holding = portfolio_store.get(symbol)

    def _fetch_and_analyze() -> str:
        data = get_data_feed().get_current_data(symbol) or {}
        return analyst.analyze(symbol, data, holding=holding)

    try:
        analysis = await run_in_threadpool(_fetch_and_analyze)
    except Exception as exc:
        logger.error("Analysis failed for %s: %s", symbol, exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"שגיאה בניתוח {symbol}") from exc
    return JSONResponse({"ticker": symbol, "analysis": analysis})


@router.post("/api/portfolio/analyze")
async def api_analyze_portfolio():
    """Whole-portfolio AI analysis, on demand.

    POST rather than GET because each call spends Anthropic tokens — this must
    never be triggered by a browser prefetch or a refresh.
    """
    analyst = get_analyst()
    if analyst is None:
        raise HTTPException(
            status_code=503,
            detail="ניתוח AI אינו מופעל — הגדר ANTHROPIC_API_KEY ו-ai.enabled: true",
        )
    try:
        report = await run_in_threadpool(_risk_analyzer.full_report)
        analysis = await run_in_threadpool(analyst.analyze_portfolio, report)
    except Exception as exc:
        logger.error("Portfolio analysis failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail="שגיאה בניתוח התיק") from exc
    return JSONResponse({"analysis": analysis})


@router.get("/api/portfolio")
async def api_portfolio():
    """Positions, ATR/sector risk reports and allocation weights.

    Runs in a worker thread: it performs blocking yfinance calls per holding.
    """
    try:
        return JSONResponse(await run_in_threadpool(_risk_analyzer.full_report))
    except Exception as exc:
        logger.error("Portfolio report failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail="שגיאה בשליפת נתוני התיק") from exc


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
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
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

  /* ── Tabs ─────────────────────────────────────────────── */
  .tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
  .tab {
    padding: 10px 16px; cursor: pointer; border: none; background: none;
    color: var(--muted); font-size: 0.9rem; font-family: inherit;
    border-bottom: 2px solid transparent;
  }
  .tab.active { color: var(--text); border-bottom-color: var(--blue); }
  .panel { display: none; }
  .panel.active { display: grid; gap: 24px; }

  /* ── Buttons ──────────────────────────────────────────── */
  button.btn {
    background: #21262d; color: var(--text); border: 1px solid var(--border);
    padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 0.8rem;
    font-family: inherit; margin-left: 4px;
  }
  button.btn:hover { background: #30363d; }
  button.btn.primary { background: #1f6feb; border-color: #1f6feb; color: #fff; }
  button.btn.primary:hover { background: #388bfd; }
  .card-actions { padding: 12px 18px; border-bottom: 1px solid var(--border); }

  /* ── Risk banners ─────────────────────────────────────── */
  .banner { padding: 14px 18px; font-size: 0.88rem; border-radius: 6px; margin-bottom: 0; }
  .banner.ok   { background: #3fb95015; border: 1px solid #3fb95055; color: var(--green); }
  .banner.warn { background: #f8514915; border: 1px solid #f8514955; color: var(--red); }

  /* ── Charts ───────────────────────────────────────────── */
  .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
  @media (max-width: 780px) { .charts { grid-template-columns: 1fr; } }
  .chart-box { padding: 18px; height: 320px; position: relative; }

  /* ── Modal ────────────────────────────────────────────── */
  .modal-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,.65);
    align-items: center; justify-content: center; z-index: 1000;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 22px; width: 320px;
  }
  .modal h3 { font-size: 1rem; margin-bottom: 14px; }
  .modal label { display: block; font-size: 0.78rem; color: var(--muted); margin: 10px 0 4px; }
  .modal input {
    width: 100%; padding: 8px 10px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text); font-size: 0.9rem; font-family: inherit;
  }
  .modal input:disabled { color: var(--muted); }
  .modal select {
    width: 100%; padding: 8px 10px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text); font-size: 0.9rem; font-family: inherit;
  }
  .muted-hint { color: var(--muted); font-weight: 400; }
  .sector-cell { cursor: pointer; border-bottom: 1px dotted var(--border); }
  .sector-cell.unknown { color: var(--yellow); }
  .split-bar { display: flex; height: 10px; border-radius: 5px; overflow: hidden; margin: 4px 0 10px; }
  .split-bar span { display: block; }
  .split-legend { display: flex; gap: 18px; font-size: 0.82rem; flex-wrap: wrap; }
  .split-legend b { font-variant-numeric: tabular-nums; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-left: 6px; }
  .modal-actions { display: flex; gap: 8px; margin-top: 18px; }
  .form-error { color: var(--red); font-size: 0.78rem; margin-top: 8px; min-height: 15px; }
  .empty { padding: 32px; text-align: center; color: var(--muted); font-size: 0.9rem; }
  .analysis-text {
    padding: 18px; white-space: pre-wrap; line-height: 1.7; font-size: 0.9rem;
    direction: rtl; text-align: right;
  }
  #build-label { color: var(--muted); font-size: 0.72rem; }
</style>
</head>
<body>
<div class="progress-bar"><div id="progress"></div></div>

<header>
  <div id="refresh-indicator"></div>
  <h1>📈 Stock Monitor</h1>
  <span id="session-badge">—</span>
  <span id="build-label"></span>
  <span id="server-time">Loading…</span>
</header>

<main>
  <div class="tabs">
    <button class="tab active" data-panel="live">מעקב חי</button>
    <button class="tab" data-panel="portfolio">התיק שלי</button>
    <button class="tab" data-panel="atr">חשיפת תנודתיות (ATR)</button>
    <button class="tab" data-panel="sector">פיזור סקטוריאלי</button>
  </div>

  <!-- ═══ Live monitoring (original view) ═══ -->
  <div id="panel-live" class="panel active">
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
  </div>

  <!-- ═══ Portfolio ═══ -->
  <div id="panel-portfolio" class="panel">
    <div class="card">
      <div class="card-title">הפוזיציות שלי</div>
      <div class="card-actions">
        <button class="btn primary" onclick="openModal()">+ הוספת פוזיציה</button>
        <button class="btn" onclick="analyzePortfolio()">🧠 ניתוח AI של התיק</button>
        <button class="btn" onclick="loadPortfolio()">רענון</button>
        <span id="portfolio-total" class="volume" style="margin-right:12px"></span>
      </div>
      <div id="holdings-wrap"><div class="empty">טוען…</div></div>
    </div>

    <div class="card" id="analysis-card" style="display:none">
      <div class="card-title" id="analysis-title">ניתוח AI</div>
      <div id="analysis-body" class="analysis-text">—</div>
    </div>

    <div class="charts">
      <div class="card">
        <div class="card-title">חלוקה לפי סקטורים</div>
        <div class="chart-box"><canvas id="sectorChart"></canvas></div>
      </div>
      <div class="card">
        <div class="card-title">חלוקה לפי מדדים</div>
        <div style="padding:14px 18px 0">
          <div class="split-bar" id="split-bar"></div>
          <div class="split-legend" id="split-legend"></div>
        </div>
        <div class="chart-box"><canvas id="indexChart"></canvas></div>
      </div>
    </div>
  </div>

  <!-- ═══ ATR volatility ═══ -->
  <div id="panel-atr" class="panel">
    <div id="atr-banner" class="banner ok">טוען…</div>
    <div class="card">
      <div class="card-title">מניות בתנודתיות גבוהה</div>
      <div id="atr-wrap"><div class="empty">—</div></div>
    </div>
  </div>

  <!-- ═══ Sector diversification ═══ -->
  <div id="panel-sector" class="panel">
    <div id="sector-banner" class="banner ok">טוען…</div>
    <div class="card">
      <div class="card-title">משקלים לפי סקטור</div>
      <div id="sector-wrap"><div class="empty">—</div></div>
    </div>
  </div>
</main>

<!-- ═══ Add / edit position modal ═══ -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <h3 id="modal-title">הזנת פוזיציה</h3>
    <label for="f-ticker">טיקר</label>
    <input id="f-ticker" placeholder="לדוגמה: AMZN" maxlength="16" autocomplete="off">
    <label for="f-qty">כמות מניות</label>
    <input id="f-qty" type="number" min="0.0001" step="any" placeholder="לדוגמה: 10">
    <label for="f-price">מחיר כניסה (למניה)</label>
    <input id="f-price" type="number" min="0.0001" step="any" placeholder="לדוגמה: 187.50">
    <label for="f-sector">סקטור <span class="muted-hint">(אופציונלי — ממלא אוטומטית אם ריק)</span></label>
    <input id="f-sector" list="sector-options" maxlength="64" placeholder="לדוגמה: Technology" autocomplete="off">
    <datalist id="sector-options">
      <option value="Technology"><option value="Healthcare"><option value="Financial Services">
      <option value="Consumer Cyclical"><option value="Consumer Defensive"><option value="Energy">
      <option value="Basic Materials"><option value="Industrials"><option value="Utilities">
      <option value="Real Estate"><option value="Communication Services">
    </datalist>
    <label for="f-asset-type">סוג נכס</label>
    <select id="f-asset-type">
      <option value="">זיהוי אוטומטי</option>
      <option value="stock">מניה בודדת</option>
      <option value="etf">מדד / קרן סל</option>
    </select>
    <div class="form-error" id="f-error"></div>
    <div class="modal-actions">
      <button class="btn primary" onclick="saveHolding()">שמירה</button>
      <button class="btn" onclick="closeModal()">ביטול</button>
    </div>
  </div>
</div>

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

/* ═══════════════════ Portfolio ═══════════════════ */

const CHART_COLORS = ['#58a6ff','#bc8cff','#3fb950','#d29922','#f85149',
                      '#39c5cf','#db61a2','#a5d6ff','#ff9f45','#8b949e'];
let sectorChart = null, indexChart = null, currentPositions = [];

const esc = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = (n) => n == null ? '—' :
  '$' + Number(n).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});

async function api(path, options = {}) {
  const res = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ── Tabs ── */
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('panel-' + tab.dataset.panel).classList.add('active');
    // Charts need a visible canvas to size correctly.
    if (tab.dataset.panel === 'portfolio' && sectorChart) {
      sectorChart.resize(); indexChart && indexChart.resize();
    }
  });
});

/* ── Modal (add + edit share one form) ── */
function openModal(existing) {
  const tickerEl = document.getElementById('f-ticker');
  document.getElementById('f-error').textContent = '';
  if (existing) {
    document.getElementById('modal-title').textContent = 'עריכת פוזיציה — ' + existing.ticker;
    tickerEl.value = existing.ticker;
    tickerEl.disabled = true;
    document.getElementById('f-qty').value = existing.quantity;
    document.getElementById('f-price').value = existing.entry_price;
    // Only a manually set sector is pre-filled; an auto-detected one stays
    // blank so saving does not silently freeze today's yfinance answer.
    document.getElementById('f-sector').value = existing.sector_is_manual ? existing.sector : '';
    document.getElementById('f-asset-type').value = existing.asset_type_is_manual ? existing.asset_type : '';
  } else {
    document.getElementById('modal-title').textContent = 'הזנת פוזיציה';
    tickerEl.value = ''; tickerEl.disabled = false;
    document.getElementById('f-qty').value = '';
    document.getElementById('f-price').value = '';
    document.getElementById('f-sector').value = '';
    document.getElementById('f-asset-type').value = '';
  }
  document.getElementById('modal').classList.add('open');
}
function closeModal() { document.getElementById('modal').classList.remove('open'); }

function editHolding(ticker) {
  const position = currentPositions.find((p) => p.ticker === ticker);
  if (position) openModal(position);
}

async function saveHolding() {
  const ticker = document.getElementById('f-ticker').value.trim().toUpperCase();
  const quantity = parseFloat(document.getElementById('f-qty').value);
  const entry_price = parseFloat(document.getElementById('f-price').value);
  const sector = document.getElementById('f-sector').value.trim() || null;
  const asset_type = document.getElementById('f-asset-type').value || null;
  const errEl = document.getElementById('f-error');
  errEl.textContent = '';
  if (!ticker) { errEl.textContent = 'יש להזין טיקר'; return; }
  if (!Number.isFinite(quantity) || quantity <= 0) { errEl.textContent = 'כמות חייבת להיות מספר חיובי'; return; }
  if (!Number.isFinite(entry_price) || entry_price <= 0) { errEl.textContent = 'מחיר כניסה חייב להיות מספר חיובי'; return; }
  try {
    await api('/api/holdings', {
      method: 'POST',
      body: JSON.stringify({ticker, quantity, entry_price, sector, asset_type}),
    });
    closeModal();
    loadPortfolio();
  } catch (e) { errEl.textContent = e.message; }
}

async function deleteHolding(ticker) {
  if (!confirm('למחוק את הפוזיציה ב-' + ticker + '?')) return;
  try {
    await api('/api/holdings/' + encodeURIComponent(ticker), {method: 'DELETE'});
    loadPortfolio();
  } catch (e) { alert(e.message); }
}

/* ── Rendering ── */
function renderHoldings(data) {
  const wrap = document.getElementById('holdings-wrap');
  if (!data.positions.length) {
    wrap.innerHTML = '<div class="empty">אין פוזיציות — הוסיפו דרך "הוספת פוזיציה"</div>';
    document.getElementById('portfolio-total').textContent = '';
    return;
  }
  const pnlCls = data.total_pnl_value >= 0 ? 'up' : 'down';
  document.getElementById('portfolio-total').innerHTML =
    `שווי תיק: <b>${money(data.total_value)}</b> · ` +
    `רווח/הפסד כולל: <span class="${pnlCls}">${money(data.total_pnl_value)}</span>`;

  wrap.innerHTML = `<table>
    <thead><tr>
      <th>סמל</th><th>כמות</th><th>מחיר כניסה</th><th>מחיר נוכחי</th>
      <th>שווי</th><th>רווח/הפסד</th><th>ATR%</th><th>סקטור</th><th></th>
    </tr></thead>
    <tbody>${data.positions.map((p) => `<tr>
      <td><span class="symbol">${esc(p.ticker)}</span></td>
      <td>${p.quantity}</td>
      <td><span class="price">${money(p.entry_price)}</span></td>
      <td><span class="price">${money(p.current_price)}</span></td>
      <td>${money(p.market_value)}</td>
      <td>${pctCell(p.pnl_pct)} <span class="volume">(${money(p.pnl_value)})</span></td>
      <td><span class="volume">${p.atr_pct == null ? '—' : p.atr_pct.toFixed(2) + '%'}</span></td>
      <td>
        <span class="sector-cell ${p.sector === 'Unknown' ? 'unknown' : ''}"
              onclick="editHolding('${esc(p.ticker)}')"
              title="לחץ לעריכת הסקטור">${esc(p.sector)}${p.sector_is_manual ? ' ✎' : ''}</span>
      </td>
      <td style="white-space:nowrap">
        <button class="btn" onclick="analyzeTicker('${esc(p.ticker)}')" title="ניתוח AI של המניה">🧠</button>
        <button class="btn" onclick="editHolding('${esc(p.ticker)}')">עריכה</button>
        <button class="btn" onclick="deleteHolding('${esc(p.ticker)}')">מחיקה</button>
      </td></tr>`).join('')}</tbody>
  </table>`;
}

function renderPie(canvasId, existing, entries) {
  const ctx = document.getElementById(canvasId);
  if (existing) existing.destroy();
  if (!entries.length) return null;
  return new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: entries.map((e) => e.label),
      datasets: [{
        data: entries.map((e) => e.weight_pct),
        backgroundColor: CHART_COLORS,
        borderColor: '#161b22',
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: {position: 'bottom', labels: {color: '#e6edf3', boxWidth: 12, font: {size: 11}}},
        tooltip: {callbacks: {label: (c) => `${c.label}: ${c.parsed.toFixed(2)}%`}},
      },
    },
  });
}

/* How much of the portfolio is held through index funds / ETFs versus picked
   as individual stocks — a different question from index *membership* below. */
function renderAssetSplit(split) {
  const bar = document.getElementById('split-bar');
  const legend = document.getElementById('split-legend');
  if (!split || (split.etf_pct === 0 && split.stock_pct === 0)) {
    bar.innerHTML = ''; legend.innerHTML = '';
    return;
  }
  bar.innerHTML =
    `<span style="width:${split.etf_pct}%;background:#bc8cff"></span>` +
    `<span style="width:${split.stock_pct}%;background:#58a6ff"></span>`;
  legend.innerHTML =
    `<span><i class="dot" style="background:#bc8cff"></i>מדדים / קרנות סל: ` +
    `<b>${split.etf_pct.toFixed(1)}%</b> <span class="volume">(${money(split.etf_value)})</span></span>` +
    `<span><i class="dot" style="background:#58a6ff"></i>מניות בודדות: ` +
    `<b>${split.stock_pct.toFixed(1)}%</b> <span class="volume">(${money(split.stock_value)})</span></span>`;
}

function renderRisk(data) {
  const vol = data.volatility, sec = data.sector;

  const atrBanner = document.getElementById('atr-banner');
  atrBanner.className = 'banner ' + (vol.alert ? 'warn' : 'ok');
  atrBanner.textContent = vol.alert
    ? `⚠️ ${vol.exposure_pct.toFixed(2)}% משווי התיק במניות בתנודתיות גבוהה (סף: ${vol.threshold_pct}%)`
    : `✅ חשיפה לתנודתיות: ${vol.exposure_pct.toFixed(2)}% מהתיק — מתחת לסף ${vol.threshold_pct}%`;
  document.getElementById('atr-wrap').innerHTML = vol.high_volatility_positions.length
    ? `<table><thead><tr><th>סמל</th><th>ATR % ממחיר</th><th>שווי פוזיציה</th></tr></thead>
       <tbody>${vol.high_volatility_positions.map((p) => `<tr>
         <td><span class="symbol">${esc(p.ticker)}</span></td>
         <td><span class="down">${p.atr_pct.toFixed(2)}%</span></td>
         <td>${money(p.market_value)}</td></tr>`).join('')}</tbody></table>`
    : '<div class="empty">אין מניות מעל סף ה-ATR</div>';

  const secBanner = document.getElementById('sector-banner');
  secBanner.className = 'banner ' + (sec.alert ? 'warn' : 'ok');
  secBanner.textContent = sec.alert
    ? `⚠️ ריכוזיות יתר: ${sec.concentrated_sectors.map((s) => s.sector + ' (' + s.weight_pct.toFixed(1) + '%)').join(', ')} — סף: ${sec.threshold_pct}%`
    : `✅ אין סקטור מעל סף הריכוזיות (${sec.threshold_pct}%)`;
  document.getElementById('sector-wrap').innerHTML = sec.sectors.length
    ? `<table><thead><tr><th>סקטור</th><th>שווי</th><th>משקל בתיק</th></tr></thead>
       <tbody>${sec.sectors.map((s) => `<tr>
         <td>${esc(s.sector)}</td><td>${money(s.market_value)}</td>
         <td>${s.weight_pct.toFixed(2)}%</td></tr>`).join('')}</tbody></table>`
    : '<div class="empty">אין נתונים</div>';
}

async function loadPortfolio() {
  try {
    const data = await api('/api/portfolio');
    currentPositions = data.positions;
    renderHoldings(data);
    renderRisk(data);
    sectorChart = renderPie('sectorChart', sectorChart, data.allocation.by_sector);
    indexChart  = renderPie('indexChart',  indexChart,  data.allocation.by_index);
    renderAssetSplit(data.allocation.by_asset_type);
  } catch (e) {
    document.getElementById('holdings-wrap').innerHTML =
      `<div class="empty" style="color:var(--red)">שגיאה: ${esc(e.message)}</div>`;
  }
}

/* ── AI analysis: one stock, or the whole portfolio ── */
async function runAnalysis(title, path, pending) {
  const card = document.getElementById('analysis-card');
  const body = document.getElementById('analysis-body');
  document.getElementById('analysis-title').textContent = title;
  card.style.display = 'block';
  body.textContent = pending;
  card.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  try {
    const data = await api(path, {method: 'POST'});
    body.textContent = data.analysis;
  } catch (e) {
    body.innerHTML = `<span class="down">שגיאה: ${esc(e.message)}</span>`;
  }
}

function analyzePortfolio() {
  return runAnalysis('ניתוח AI — התיק כמכלול', '/api/portfolio/analyze',
                     'מנתח את התיק… (עשוי לקחת עד דקה)');
}

function analyzeTicker(ticker) {
  return runAnalysis(`ניתוח AI — ${ticker}`, '/api/analyze/' + encodeURIComponent(ticker),
                     `מנתח את ${ticker}… (עשוי לקחת עד דקה)`);
}

/* ── Build label: makes a stale deploy obvious instead of invisible ── */
async function loadBuild() {
  try {
    const b = await api('/health');
    document.getElementById('build-label').textContent = `v${b.version} · ${b.revision}`;
  } catch (_) { /* diagnostics only */ }
}

document.getElementById('modal').addEventListener('click', (e) => {
  if (e.target.id === 'modal') closeModal();
});

refresh();
loadBuild();
loadPortfolio();
setInterval(refresh, INTERVAL);
setInterval(loadPortfolio, 120000);   // portfolio prices refresh every 2 min
tickProgress();
</script>
</body>
</html>
"""
