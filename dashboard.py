"""Dashboard UI: a single self-contained HTML page served by FastAPI.

Includes:
- Manual entry modal (quantity + purchase date per ticker) -> /api/holdings
- Alert tabs: ATR volatility exposure and sector diversification
- Chart.js pie charts: sector allocation and index allocation
- On-demand AI analysis per position
"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stock Tracker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0e1117; color: #e6e6e6; min-height: 100vh;
  }
  .container { max-width: 1100px; margin: 0 auto; padding: 24px; }
  h1 {
    font-size: 26px; margin-bottom: 20px;
    background: linear-gradient(135deg, #4f8cff, #9b6bff);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }
  .toolbar { display: flex; gap: 10px; margin-bottom: 18px; }
  button {
    background: #1d2735; color: #e6e6e6; border: 1px solid #33415c;
    padding: 8px 14px; border-radius: 8px; cursor: pointer; font-size: 14px;
  }
  button:hover { background: #27354a; }
  button.primary { background: #2b5cd9; border-color: #2b5cd9; }
  button.primary:hover { background: #3a6ce8; }
  .tabs { display: flex; gap: 4px; border-bottom: 1px solid #33415c; margin-bottom: 16px; }
  .tab {
    padding: 10px 16px; cursor: pointer; border: none; background: none;
    color: #9aa4b2; font-size: 15px; border-bottom: 2px solid transparent;
  }
  .tab.active { color: #fff; border-bottom-color: #4f8cff; }
  .panel { display: none; }
  .panel.active { display: block; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 20px; }
  th, td { padding: 10px 8px; text-align: right; border-bottom: 1px solid #232d3f; font-size: 14px; }
  th { color: #9aa4b2; font-weight: 600; }
  .pos { color: #4ade80; } .neg { color: #f87171; }
  .badge { padding: 3px 10px; border-radius: 999px; font-size: 12px; }
  .badge.ok { background: #113a24; color: #4ade80; }
  .badge.warn { background: #451a1a; color: #f87171; }
  .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
  @media (max-width: 760px) { .charts { grid-template-columns: 1fr; } }
  .card { background: #161c27; border: 1px solid #232d3f; border-radius: 12px; padding: 18px; margin-bottom: 18px; }
  .card h3 { font-size: 16px; margin-bottom: 12px; color: #c9d4e3; }
  .alert-banner { padding: 12px 16px; border-radius: 10px; margin-bottom: 14px; font-size: 14px; }
  .alert-banner.ok { background: #10241a; border: 1px solid #1f5138; }
  .alert-banner.warn { background: #2b1414; border: 1px solid #7f1d1d; }
  /* modal */
  .modal-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,.6);
    align-items: center; justify-content: center; z-index: 50;
  }
  .modal-overlay.open { display: flex; }
  .modal { background: #161c27; border: 1px solid #33415c; border-radius: 14px; padding: 24px; width: 340px; }
  .modal h3 { margin-bottom: 16px; }
  .modal label { display: block; font-size: 13px; color: #9aa4b2; margin: 10px 0 4px; }
  .modal input {
    width: 100%; padding: 8px 10px; border-radius: 8px; border: 1px solid #33415c;
    background: #0e1117; color: #e6e6e6; font-size: 14px;
  }
  .modal .actions { display: flex; gap: 8px; margin-top: 18px; justify-content: flex-start; }
  .error-msg { color: #f87171; font-size: 13px; margin-top: 8px; min-height: 16px; }
  .analysis { white-space: pre-wrap; font-size: 14px; line-height: 1.6; }
  .muted { color: #9aa4b2; }
  .spinner { color: #9aa4b2; font-size: 14px; }
</style>
</head>
<body>
<div class="container">
  <h1>Stock Tracker — דשבורד תיק אישי</h1>

  <div class="toolbar">
    <button class="primary" onclick="openModal()">+ הוספת פוזיציה</button>
    <button onclick="refreshAll()">רענון</button>
  </div>

  <div class="tabs">
    <button class="tab active" data-panel="overview">סקירת תיק</button>
    <button class="tab" data-panel="atr">חשיפת תנודתיות (ATR)</button>
    <button class="tab" data-panel="sector">פיזור סקטוריאלי</button>
  </div>

  <!-- ============ Overview ============ -->
  <div id="panel-overview" class="panel active">
    <div class="card">
      <h3>פוזיציות</h3>
      <table>
        <thead><tr>
          <th>טיקר</th><th>כמות</th><th>תאריך רכישה</th><th>מחיר כניסה</th>
          <th>מחיר נוכחי</th><th>שווי</th><th>רווח/הפסד</th><th></th>
        </tr></thead>
        <tbody id="positions-body">
          <tr><td colspan="8" class="muted">טוען…</td></tr>
        </tbody>
      </table>
    </div>
    <div class="charts">
      <div class="card"><h3>חלוקה לפי סקטורים</h3><canvas id="sectorChart"></canvas></div>
      <div class="card"><h3>חלוקה לפי מדדים</h3><canvas id="indexChart"></canvas></div>
    </div>
    <div class="card" id="analysis-card" style="display:none">
      <h3 id="analysis-title">ניתוח AI</h3>
      <div id="analysis-body" class="analysis"></div>
    </div>
  </div>

  <!-- ============ ATR tab ============ -->
  <div id="panel-atr" class="panel">
    <div id="atr-banner" class="alert-banner ok">טוען…</div>
    <div class="card">
      <h3>מניות בתנודתיות גבוהה (ATR% מעל הסף)</h3>
      <table>
        <thead><tr><th>טיקר</th><th>ATR % ממחיר</th><th>שווי פוזיציה</th></tr></thead>
        <tbody id="atr-body"><tr><td colspan="3" class="muted">—</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- ============ Sector tab ============ -->
  <div id="panel-sector" class="panel">
    <div id="sector-banner" class="alert-banner ok">טוען…</div>
    <div class="card">
      <h3>פירוט משקלים לפי סקטור</h3>
      <table>
        <thead><tr><th>סקטור</th><th>שווי</th><th>משקל בתיק</th></tr></thead>
        <tbody id="sector-body"><tr><td colspan="3" class="muted">—</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<!-- ============ Manual entry modal ============ -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <h3>הזנת פוזיציה ידנית</h3>
    <label for="f-ticker">טיקר</label>
    <input id="f-ticker" placeholder="לדוגמה: AAPL" maxlength="12">
    <label for="f-qty">כמות מניות</label>
    <input id="f-qty" type="number" min="0.0001" step="any" placeholder="לדוגמה: 10">
    <label for="f-date">מועד רכישה</label>
    <input id="f-date" type="date">
    <div class="error-msg" id="f-error"></div>
    <div class="actions">
      <button class="primary" onclick="saveHolding()">שמירה</button>
      <button onclick="closeModal()">ביטול</button>
    </div>
  </div>
</div>

<script>
"use strict";

const CHART_COLORS = ["#4f8cff","#9b6bff","#34d399","#fbbf24","#f87171",
                      "#38bdf8","#f472b6","#a3e635","#fb923c","#94a3b8"];
let sectorChart = null, indexChart = null;

/* ---------- helpers ---------- */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}
const fmt = (n, d = 2) =>
  n === null || n === undefined ? "—" : Number(n).toLocaleString("en-US",
    { minimumFractionDigits: d, maximumFractionDigits: d });
const esc = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById("panel-" + tab.dataset.panel).classList.add("active");
  });
});

/* ---------- modal ---------- */
function openModal() {
  document.getElementById("f-error").textContent = "";
  document.getElementById("modal").classList.add("open");
}
function closeModal() { document.getElementById("modal").classList.remove("open"); }

async function saveHolding() {
  const ticker = document.getElementById("f-ticker").value.trim().toUpperCase();
  const quantity = parseFloat(document.getElementById("f-qty").value);
  const purchase_date = document.getElementById("f-date").value;
  const errEl = document.getElementById("f-error");
  errEl.textContent = "";
  if (!ticker) { errEl.textContent = "יש להזין טיקר"; return; }
  if (!Number.isFinite(quantity) || quantity <= 0) { errEl.textContent = "כמות חייבת להיות מספר חיובי"; return; }
  if (!purchase_date) { errEl.textContent = "יש לבחור מועד רכישה"; return; }
  try {
    await api("/api/holdings", {
      method: "POST",
      body: JSON.stringify({ ticker, quantity, purchase_date }),
    });
    closeModal();
    refreshAll();
  } catch (e) { errEl.textContent = e.message; }
}

async function deleteHolding(ticker) {
  if (!confirm("למחוק את הפוזיציה ב-" + ticker + "?")) return;
  try { await api("/api/holdings/" + encodeURIComponent(ticker), { method: "DELETE" }); refreshAll(); }
  catch (e) { alert(e.message); }
}

/* ---------- overview ---------- */
async function loadPortfolio() {
  const body = document.getElementById("positions-body");
  try {
    const data = await api("/api/portfolio/summary");
    if (!data.positions.length) {
      body.innerHTML = '<tr><td colspan="8" class="muted">אין פוזיציות — הוסיפו דרך "הוספת פוזיציה"</td></tr>';
      return;
    }
    body.innerHTML = data.positions.map((p) => {
      const cls = (p.pnl_pct ?? 0) >= 0 ? "pos" : "neg";
      const pnl = p.pnl_pct === null ? "—" :
        `<span class="${cls}">${fmt(p.pnl_pct)}% (${fmt(p.pnl_value)})</span>`;
      return `<tr>
        <td><b>${esc(p.ticker)}</b></td><td>${fmt(p.quantity, 4)}</td>
        <td>${esc(p.purchase_date)}</td><td>${fmt(p.entry_price)}</td>
        <td>${fmt(p.current_price)}</td><td>${fmt(p.market_value)}</td>
        <td>${pnl}</td>
        <td>
          <button onclick="analyze('${esc(p.ticker)}')">ניתוח AI</button>
          <button onclick="deleteHolding('${esc(p.ticker)}')">מחיקה</button>
        </td></tr>`;
    }).join("");
  } catch (e) {
    body.innerHTML = `<tr><td colspan="8" class="neg">שגיאה: ${esc(e.message)}</td></tr>`;
  }
}

/* ---------- allocation pie charts ---------- */
function renderPie(canvasId, existing, labels, values) {
  const ctx = document.getElementById(canvasId);
  if (existing) existing.destroy();
  return new Chart(ctx, {
    type: "pie",
    data: {
      labels,
      datasets: [{ data: values, backgroundColor: CHART_COLORS, borderColor: "#0e1117", borderWidth: 2 }],
    },
    options: {
      plugins: {
        legend: { position: "bottom", labels: { color: "#c9d4e3" } },
        tooltip: { callbacks: { label: (c) => `${c.label}: ${fmt(c.parsed)}%` } },
      },
    },
  });
}

async function loadAllocation() {
  try {
    const data = await api("/api/allocation");
    sectorChart = renderPie("sectorChart", sectorChart,
      data.by_sector.map((s) => s.label), data.by_sector.map((s) => s.weight_pct));
    indexChart = renderPie("indexChart", indexChart,
      data.by_index.map((s) => s.label), data.by_index.map((s) => s.weight_pct));
  } catch (e) { console.error("allocation load failed:", e); }
}

/* ---------- alerts ---------- */
async function loadAlerts() {
  try {
    const data = await api("/api/alerts");
    const vol = data.volatility, sec = data.sector;

    const atrBanner = document.getElementById("atr-banner");
    atrBanner.className = "alert-banner " + (vol.alert ? "warn" : "ok");
    atrBanner.textContent = vol.alert
      ? `⚠️ ${fmt(vol.exposure_pct)}% משווי התיק במניות בתנודתיות גבוהה (סף: ${vol.threshold_pct}%)`
      : `✅ חשיפה לתנודתיות: ${fmt(vol.exposure_pct)}% מהתיק (מתחת לסף ${vol.threshold_pct}%)`;
    document.getElementById("atr-body").innerHTML =
      vol.high_volatility_positions.length
        ? vol.high_volatility_positions.map((p) =>
            `<tr><td>${esc(p.ticker)}</td><td>${fmt(p.atr_pct)}%</td><td>${fmt(p.market_value)}</td></tr>`).join("")
        : '<tr><td colspan="3" class="muted">אין מניות מעל סף ה-ATR</td></tr>';

    const secBanner = document.getElementById("sector-banner");
    secBanner.className = "alert-banner " + (sec.alert ? "warn" : "ok");
    secBanner.textContent = sec.alert
      ? `⚠️ ריכוזיות יתר: ${sec.concentrated_sectors.map((s) => `${s.sector} (${fmt(s.weight_pct)}%)`).join(", ")} — סף: ${sec.threshold_pct}%`
      : `✅ אין סקטור מעל סף הריכוזיות (${sec.threshold_pct}%)`;
    document.getElementById("sector-body").innerHTML =
      sec.sectors.length
        ? sec.sectors.map((s) =>
            `<tr><td>${esc(s.sector)}</td><td>${fmt(s.market_value)}</td><td>${fmt(s.weight_pct)}%</td></tr>`).join("")
        : '<tr><td colspan="3" class="muted">אין נתונים</td></tr>';
  } catch (e) {
    document.getElementById("atr-banner").textContent = "שגיאה בטעינת התראות: " + e.message;
    document.getElementById("sector-banner").textContent = "שגיאה בטעינת התראות: " + e.message;
  }
}

/* ---------- AI analysis ---------- */
async function analyze(ticker) {
  const card = document.getElementById("analysis-card");
  const body = document.getElementById("analysis-body");
  document.getElementById("analysis-title").textContent = "ניתוח AI — " + ticker;
  card.style.display = "block";
  body.innerHTML = '<span class="spinner">מריץ ניתוח… (עד דקה)</span>';
  card.scrollIntoView({ behavior: "smooth" });
  try {
    const data = await api("/api/analyze/" + encodeURIComponent(ticker));
    body.textContent = data.analysis;
  } catch (e) {
    body.innerHTML = `<span class="neg">שגיאה: ${esc(e.message)}</span>`;
  }
}

/* ---------- boot ---------- */
function refreshAll() { loadPortfolio(); loadAllocation(); loadAlerts(); }
document.getElementById("modal").addEventListener("click", (e) => {
  if (e.target.id === "modal") closeModal();
});
refreshAll();
setInterval(refreshAll, 120000); // refresh every 2 minutes
</script>
</body>
</html>
"""


def render_dashboard() -> str:
    """Return the dashboard page HTML."""
    return DASHBOARD_HTML
