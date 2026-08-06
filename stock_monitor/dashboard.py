"""
Web dashboard — served at http://localhost:8080/

Shows live stock prices, recent alerts, the personal portfolio (manually
entered positions with entry price), ATR volatility and sector-concentration
risk tabs, and allocation pie charts.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List

import pandas as pd
from fastapi import APIRouter, Body, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import excel_export
from .portfolio_risk import PortfolioRiskAnalyzer
from .store import (
    ClosedPosition,
    Holding,
    HoldingError,
    alert_store,
    closed_position_store,
    portfolio_store,
    watchlist_store,
)

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
    # The analyzer needs it too, for the pre/post-market columns.
    _risk_analyzer.set_data_feed(feed)


def get_data_feed():
    """The app's feed when running as a daemon; a standalone one otherwise."""
    global _data_feed
    if _data_feed is None:
        from .data_feed import StockDataFeed

        set_data_feed(StockDataFeed())
    return _data_feed


_news_monitor = None


def set_news_monitor(monitor) -> None:
    global _news_monitor
    _news_monitor = monitor


def get_news_monitor():
    global _news_monitor
    if _news_monitor is None:
        from .news_monitor import NewsMonitor

        _news_monitor = NewsMonitor(portfolio_store)
    return _news_monitor

# ── Shared live-price cache (written by monitor_cycle, read by dashboard) ────
_price_cache: Dict[str, dict] = {}


def prune_price_cache(symbols) -> None:
    """Drop cached quotes for symbols no longer watched.

    Without this a symbol removed from the watchlist would keep showing its
    last price on the live tab forever, since nothing else ever evicts it.
    """
    keep = {str(s).upper() for s in symbols}
    for symbol in [s for s in _price_cache if s not in keep]:
        _price_cache.pop(symbol, None)


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
            purchase_date=payload.get("purchase_date"),
        )
    except HoldingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    portfolio_store.upsert(holding)
    return JSONResponse(holding.as_dict(), status_code=201)


@router.post("/api/holdings/{ticker}/add")
async def api_add_to_holding(ticker: str, payload: dict = Body(...)):
    """Buy more of a position you already hold.

    Separate from the upsert on purpose: that one replaces, which is right for
    correcting a mistake and wrong for topping up. Here the caller sends only
    what they bought and the weighted average is computed for them.
    """
    holding = portfolio_store.get(ticker)
    if holding is None:
        raise HTTPException(status_code=404, detail="הפוזיציה לא נמצאה")

    try:
        updated = holding.add_shares(
            quantity=payload.get("quantity"),
            price=payload.get("price"),
            purchase_date=payload.get("purchase_date"),
        )
    except HoldingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    portfolio_store.upsert(updated)
    return JSONResponse(
        {
            "holding": updated.as_dict(),
            "previous": {"quantity": holding.quantity, "entry_price": holding.entry_price},
        },
        status_code=201,
    )


@router.delete("/api/holdings/{ticker}")
async def api_delete_holding(ticker: str):
    if not portfolio_store.delete(ticker):
        raise HTTPException(status_code=404, detail="הפוזיציה לא נמצאה")
    return JSONResponse({"status": "deleted", "ticker": ticker.strip().upper()})


# ── Trade journal ─────────────────────────────────────────────────────────────

@router.post("/api/holdings/{ticker}/sell")
async def api_sell_holding(ticker: str, payload: dict = Body(...)):
    """Record a sale. Selling the whole quantity closes the position.

    A partial sale reduces the remaining holding and still writes a journal
    entry for the portion sold, so averaging out is recorded trade by trade.
    """
    holding = portfolio_store.get(ticker)
    if holding is None:
        raise HTTPException(status_code=404, detail="הפוזיציה לא נמצאה")

    # ATR and sector are captured now: after the position is gone there is no
    # way to reconstruct how volatile the stock was while it was held.
    atr_pct = sector = None
    try:
        get_data_feed()   # makes sure the analyzer can price the position
        snapshot = await run_in_threadpool(_risk_analyzer.collect_positions)
        current = next((p for p in snapshot if p["ticker"] == holding.ticker), None)
        if current:
            atr_pct, sector = current.get("atr_pct"), current.get("sector")
    except Exception as exc:
        logger.warning("Could not snapshot risk data for %s: %s", holding.ticker, exc)

    try:
        closed = ClosedPosition.from_sale(
            holding=holding,
            quantity_sold=payload.get("quantity"),
            exit_price=payload.get("exit_price"),
            sold_date=payload.get("sold_date"),
            atr_pct=atr_pct,
            sector=sector,
        )
    except HoldingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    closed_position_store.add(closed)

    remaining = holding.quantity - closed.quantity
    if remaining > 1e-9:
        portfolio_store.upsert(
            Holding(
                ticker=holding.ticker,
                quantity=remaining,
                entry_price=holding.entry_price,
                sector=holding.sector,
                asset_type=holding.asset_type,
                purchase_date=holding.purchase_date,
            )
        )
    else:
        portfolio_store.delete(holding.ticker)

    return JSONResponse(
        {"closed": closed.as_dict(), "remaining_quantity": round(max(remaining, 0.0), 6)},
        status_code=201,
    )


@router.get("/api/journal")
async def api_journal():
    entries = [e.as_dict() for e in closed_position_store.all()]
    wins = [e for e in entries if e["pnl_pct"] > 0]
    return JSONResponse({
        "entries": entries,
        "summary": {
            "count": len(entries),
            "total_pnl": round(sum(e["pnl_value"] for e in entries), 2),
            "win_rate_pct": round(len(wins) / len(entries) * 100.0, 1) if entries else 0.0,
            "avg_holding_days": (
                round(
                    sum(e["holding_days"] for e in entries if e["holding_days"] is not None)
                    / max(sum(1 for e in entries if e["holding_days"] is not None), 1)
                )
                if any(e["holding_days"] is not None for e in entries) else None
            ),
        },
    })


@router.post("/api/journal/{entry_id}/analyze")
async def api_analyze_closed_position(entry_id: int):
    """Have Claude grade a closed trade; the result is stored, not recomputed."""
    analyst = get_analyst()
    if analyst is None:
        raise HTTPException(
            status_code=503,
            detail="ניתוח AI אינו מופעל — הגדר ANTHROPIC_API_KEY ו-ai.enabled: true",
        )
    entry = closed_position_store.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="הרשומה לא נמצאה")

    try:
        review = await run_in_threadpool(analyst.review_closed_position, entry.as_dict())
    except Exception as exc:
        logger.error("Closed-position review failed for %s: %s", entry_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="שגיאה בניתוח העסקה") from exc

    updated = closed_position_store.update_fields(
        entry_id, rating=review["rating"], ai_analysis=review["explanation"]
    )
    return JSONResponse((updated or entry).as_dict())


@router.patch("/api/journal/{entry_id}")
async def api_update_journal_entry(entry_id: int, payload: dict = Body(...)):
    """Save the user's own note. Deliberately the only user-writable field."""
    if "personal_note" not in payload:
        raise HTTPException(status_code=422, detail="ניתן לעדכן רק את חוות הדעת האישית")
    note = payload["personal_note"]
    note = str(note).strip() if note is not None else None
    updated = closed_position_store.update_fields(entry_id, personal_note=note or None)
    if updated is None:
        raise HTTPException(status_code=404, detail="הרשומה לא נמצאה")
    return JSONResponse(updated.as_dict())


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
        get_data_feed()   # attaches the feed that supplies pre/post-market data
        return JSONResponse(await run_in_threadpool(_risk_analyzer.full_report))
    except Exception as exc:
        logger.error("Portfolio report failed: %s", exc, exc_info=True)
        # The exception class goes to the browser too. A bare "שגיאה" left both
        # the user and the logs-less browser with nothing to act on; the type
        # name carries no secrets and is often the whole diagnosis.
        raise HTTPException(
            status_code=502,
            detail=f"שגיאה בשליפת נתוני התיק ({exc.__class__.__name__})",
        ) from exc


# ── Watchlist ─────────────────────────────────────────────────────────────────

@router.get("/api/watchlist")
async def api_watchlist():
    return JSONResponse({"symbols": watchlist_store.all()})


@router.post("/api/watchlist")
async def api_add_watchlist(payload: dict = Body(...)):
    try:
        symbol = watchlist_store.add(payload.get("symbol", ""))
    except HoldingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"symbols": watchlist_store.all(), "added": symbol}, status_code=201)


@router.delete("/api/watchlist/{symbol}")
async def api_remove_watchlist(symbol: str):
    if not watchlist_store.remove(symbol):
        raise HTTPException(status_code=404, detail="הסמל לא נמצא ברשימת המעקב")
    return JSONResponse({"symbols": watchlist_store.all()})


# ── Breaking news ─────────────────────────────────────────────────────────────

@router.get("/api/news")
async def api_news(max_age_minutes: int = 60):
    """Recent headlines for held tickers, from the pre-market scan cache.

    ``max_age_minutes=0`` forces a fresh scan — that is the "סריקה מחדש"
    button. The default serves the cache so opening the tab is instant.
    """
    monitor = get_news_monitor()
    try:
        return JSONResponse(
            await run_in_threadpool(monitor.snapshot, max(max_age_minutes, 0))
        )
    except Exception as exc:
        logger.error("News snapshot failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail="שגיאה בשליפת החדשות") from exc


# ── Excel export ──────────────────────────────────────────────────────────────

@router.get("/api/portfolio/export")
async def api_export_portfolio():
    """The portfolio and the trade journal as a downloadable .xlsx.

    Built from the same report the dashboard renders, so the sheet cannot drift
    from the screen. Runs in a worker thread: the report does blocking network
    work and the workbook is assembled in memory.
    """
    def _build() -> bytes:
        get_data_feed()
        report = _risk_analyzer.full_report()
        entries = [e.as_dict() for e in closed_position_store.all()]
        wins = [e for e in entries if e["pnl_pct"] > 0]
        with_days = [e["holding_days"] for e in entries if e["holding_days"] is not None]
        journal = {
            "entries": entries,
            "summary": {
                "count": len(entries),
                "total_pnl": round(sum(e["pnl_value"] for e in entries), 2),
                "win_rate_pct": round(len(wins) / len(entries) * 100.0, 1) if entries else 0.0,
                "avg_holding_days": round(sum(with_days) / len(with_days)) if with_days else None,
            },
        }
        return excel_export.build_workbook(report, journal)

    try:
        content = await run_in_threadpool(_build)
    except Exception as exc:
        logger.error("Excel export failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=502, detail=f"יצירת קובץ האקסל נכשלה ({exc.__class__.__name__})"
        ) from exc

    name = excel_export.filename()
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ── Diagnostics ───────────────────────────────────────────────────────────────

@router.get("/api/diagnostics/{ticker}")
async def api_diagnostics(ticker: str, period: str = "1mo"):
    """What each Yahoo endpoint actually returns for one ticker, right now.

    Price problems here are environment-specific: the deployed host gets
    different answers from Yahoo than a laptop does, and neither the logs nor
    the dashboard show which of the three sources disagrees. This puts the raw
    bar dates side by side so the question is settled with data instead of
    guesswork. Read-only, one ticker, on demand — and behind the dashboard
    password like every other API route.
    """
    try:
        symbol = Holding.create(ticker, 1, 1).ticker
    except HoldingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _probe() -> dict:
        import yfinance as yf

        from .data_feed import get_market_session

        out: Dict[str, object] = {
            "ticker": symbol,
            "server_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "market_session": get_market_session(),
            "yfinance_version": getattr(yf, "__version__", "unknown"),
        }

        def _tail(frame, source: str) -> None:
            """Last five bars as (date, close), or why there are none."""
            try:
                if frame is None or getattr(frame, "empty", True):
                    out[source] = "empty"
                    return
                if "Close" not in frame.columns:
                    out[source] = f"no Close column: {list(frame.columns)[:6]}"
                    return
                closes = frame["Close"].tail(5)
                stamp = (lambda i: str(i)) if source == "intraday_5m" else (
                    lambda i: str(i.date() if hasattr(i, "date") else i))
                out[source] = [
                    [stamp(idx),
                     None if pd.isna(v) else round(float(v), 2)]
                    for idx, v in closes.items()
                ]
                out[f"{source}_index_tz"] = str(getattr(frame.index, "tz", None))
            except Exception as exc:
                out[source] = f"{exc.__class__.__name__}: {exc}"

        try:
            batch = yf.download([symbol], period=period, auto_adjust=True,
                                progress=False, group_by="ticker", threads=False)
            if batch is not None and not batch.empty and hasattr(batch.columns, "levels"):
                batch = batch[symbol] if symbol in batch.columns.get_level_values(0) else batch
            _tail(batch, "download")
        except Exception as exc:
            out["download"] = f"{exc.__class__.__name__}: {exc}"

        try:
            _tail(yf.Ticker(symbol).history(period=period, auto_adjust=True), "history")
        except Exception as exc:
            out["history"] = f"{exc.__class__.__name__}: {exc}"

        # The intraday series is what the portfolio now prices from during a
        # session, so its freshness is the question when prices look stuck.
        try:
            intraday = yf.download([symbol], period="5d", interval="5m",
                                   prepost=True, auto_adjust=True,
                                   progress=False, group_by="ticker", threads=False)
            if (intraday is not None and not intraday.empty
                    and hasattr(intraday.columns, "levels")
                    and symbol in intraday.columns.get_level_values(0)):
                intraday = intraday[symbol]
            _tail(intraday, "intraday_5m")
            if isinstance(out.get("intraday_5m"), list) and out["intraday_5m"]:
                newest = out["intraday_5m"][-1][0]
                out["intraday_newest_bar"] = newest
                # Compared in exchange time: between 00:00 and 04:00 UTC the
                # UTC date is already tomorrow while New York is still today.
                from .data_feed import NYSE_TZ

                out["intraday_covers_today"] = (
                    str(datetime.now(NYSE_TZ).date()) in newest
                )
        except Exception as exc:
            out["intraday_5m"] = f"{exc.__class__.__name__}: {exc}"

        try:
            fast = yf.Ticker(symbol).fast_info
            out["fast_info"] = {
                name: getattr(fast, name, None)
                for name in ("last_price", "previous_close", "last_volume")
            }
        except Exception as exc:
            out["fast_info"] = f"{exc.__class__.__name__}: {exc}"

        return out

    try:
        return JSONResponse(await run_in_threadpool(_probe))
    except Exception as exc:
        logger.error("Diagnostics failed for %s: %s", symbol, exc, exc_info=True)
        raise HTTPException(
            status_code=502, detail=f"אבחון נכשל ({exc.__class__.__name__})"
        ) from exc


# ── Dashboard HTML ────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=_HTML)


_HTML = """<!DOCTYPE html>
<html lang="he" dir="rtl">
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
  #server-time { margin-inline-start: auto; color: var(--muted); font-size: 0.8rem; }
  #refresh-indicator { width: 8px; height: 8px; border-radius: 50%; background: var(--green); }
  /* 1320, not 1200: the portfolio table carries eleven columns plus the row
     actions and needed the extra width to fit without a horizontal scroll. */
  main { padding: 24px; display: grid; gap: 24px; max-width: 1320px; margin: 0 auto; }

  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .card-title { padding: 14px 18px; font-size: 0.85rem; font-weight: 600;
    color: var(--muted); border-bottom: 1px solid var(--border); text-transform: uppercase; letter-spacing: .05em; }

  /* The card clips overflow, so a wide table has to scroll inside its own
     wrapper — otherwise the last columns (the row actions) are cut off on a
     narrow window or a phone. */
  #stocks-wrap, #alerts-wrap, #holdings-wrap, #atr-wrap, #sector-wrap { overflow-x: auto; }
  /* max-content, not 100%: the row-action buttons cannot wrap, so a table
     pinned to the container width has them clipped instead of scrolled. */
  #holdings-wrap table { width: max-content; min-width: 100%; }
  #holdings-wrap td, #holdings-wrap th { padding-inline: 12px; }
  /* Icons rather than labels: with eleven columns the words pushed the actions
     off-screen. Each button carries a title, so hovering still explains it. */
  .row-actions { white-space: nowrap; }
  .row-actions .btn { padding: 5px 8px; margin-inline-start: 2px; font-size: 0.9rem; }
  /* The page is RTL, but most of what it shows is not: tickers, prices and
     percentages are Latin/numeric. `plaintext` picks each element's direction
     from its own first strong character, so "45 ימים" reads RTL while
     "-$134.00" and "AMZN" read LTR. Without it a leading currency sign or
     minus is treated as neutral and lands on the wrong end of the number.
     Table *cells* are excluded: they hold several elements whose order must
     follow the page, so the first row-action button stays rightmost. Header
     cells are single strings with no such ordering, and need it — "ATR%"
     otherwise renders as "%ATR". */
  th, .chip, .entry-stats b, .summary b, .news-meta, .split-legend span,
  #portfolio-total {
    unicode-bidi: plaintext;
  }
  /* Always LTR, never auto-detected: a timestamp ending in "UTC" has its only
     strong character at the end, which flips the whole string. */
  #server-time, #build-label, .alert-ts { direction: ltr; unicode-bidi: isolate; }
  /* Ticker and amount fields are typed left-to-right whatever the page does. */
  input[type="number"], input[type="date"], #f-ticker, #w-symbol {
    direction: ltr; text-align: left;
  }

  table { width: 100%; border-collapse: collapse; }
  th { padding: 10px 18px; text-align: start; font-size: 0.75rem; color: var(--muted);
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
    font-family: inherit; margin-inline-start: 4px;
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
  .index-detail { color: var(--muted); font-size: 0.78rem; margin-top: 10px; }
  .split-legend { display: flex; gap: 18px; font-size: 0.82rem; flex-wrap: wrap; }
  .split-legend b { font-variant-numeric: tabular-nums; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-inline-end: 6px; }
  .modal-actions { display: flex; gap: 8px; margin-top: 18px; }
  .form-error { color: var(--red); font-size: 0.78rem; margin-top: 8px; min-height: 15px; }
  .empty { padding: 32px; text-align: center; color: var(--muted); font-size: 0.9rem; }
  .analysis-text { padding: 18px; white-space: pre-wrap; line-height: 1.7; font-size: 0.9rem; }
  #build-label { color: var(--muted); font-size: 0.72rem; }

  /* ── Watchlist chips ──────────────────────────────────── */
  .chips { display: flex; flex-wrap: wrap; gap: 8px; padding: 14px 18px; }
  .chip {
    display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px;
    border-radius: 20px; background: #1f6feb22; border: 1px solid #1f6feb55;
    color: var(--blue); font-size: 0.82rem; font-weight: 600;
  }
  .chip button {
    background: none; border: none; color: var(--muted); cursor: pointer;
    font-size: 0.95rem; line-height: 1; padding: 0; font-family: inherit;
  }
  .chip button:hover { color: var(--red); }
  .inline-form { display: flex; gap: 8px; padding: 0 18px 14px; flex-wrap: wrap; }
  .inline-form input {
    padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text); font-size: 0.85rem;
    font-family: inherit; width: 150px;
  }

  /* ── Trade journal ────────────────────────────────────── */
  .summary { display: flex; flex-wrap: wrap; gap: 26px; padding: 16px 18px; }
  .summary div { font-size: 0.82rem; color: var(--muted); }
  .summary b { display: block; font-size: 1.25rem; color: var(--text);
    font-variant-numeric: tabular-nums; margin-top: 3px; }
  .journal { display: grid; gap: 16px; }
  .entry { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; }
  .entry-head {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 14px 18px; border-bottom: 1px solid var(--border);
  }
  .entry-head .grow { flex: 1; }
  .light {
    display: inline-block; width: 12px; height: 12px; border-radius: 50%;
    border: 1px solid #0006; flex-shrink: 0;
  }
  .light-green  { background: var(--green); box-shadow: 0 0 8px #3fb95088; }
  .light-orange { background: var(--yellow); box-shadow: 0 0 8px #d2992288; }
  .light-red    { background: var(--red);   box-shadow: 0 0 8px #f8514988; }
  .light-none   { background: var(--border); }
  .badge {
    padding: 2px 8px; border-radius: 4px; font-size: 0.72rem; font-weight: 600;
    background: #d2992222; color: var(--yellow);
  }
  .entry-stats {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
    gap: 12px; padding: 14px 18px; border-bottom: 1px solid #21262d;
  }
  .entry-stats span { font-size: 0.75rem; color: var(--muted); display: block; }
  .entry-stats b { font-size: 0.95rem; font-variant-numeric: tabular-nums; }
  .entry-section { padding: 14px 18px; border-bottom: 1px solid #21262d; }
  .entry-section:last-child { border-bottom: none; }
  .entry-section h4 {
    font-size: 0.75rem; color: var(--muted); text-transform: uppercase;
    letter-spacing: .05em; margin-bottom: 8px; font-weight: 600;
  }
  .entry-section .body { white-space: pre-wrap; line-height: 1.7; font-size: 0.88rem; }
  textarea.note {
    width: 100%; min-height: 70px; padding: 9px 11px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg); color: var(--text);
    font-size: 0.88rem; font-family: inherit; resize: vertical;
  }
  .save-hint { font-size: 0.75rem; color: var(--green); margin-inline-start: 8px; }

  /* ── News ─────────────────────────────────────────────── */
  .news-item { padding: 13px 18px; border-bottom: 1px solid #21262d; }
  .news-item:last-child { border-bottom: none; }
  .news-item a { color: var(--text); text-decoration: none; font-size: 0.9rem; }
  .news-item a:hover { color: var(--blue); text-decoration: underline; }
  .news-meta { color: var(--muted); font-size: 0.76rem; margin-top: 4px; }
  .news-fresh { color: var(--yellow); font-weight: 600; }
  .ext-price { font-size: 0.78rem; }
  /* Under the price rather than beside it: as its own column the as-of date
     pushed the row actions off the edge. */
  .price-date { font-size: 0.72rem; margin-top: 2px; white-space: nowrap; }
  /* The averaged result, shown before committing: the arithmetic is the whole
     point of the dialog, so it should be visible rather than taken on trust. */
  .preview {
    margin-top: 14px; padding: 10px 12px; border-radius: 6px;
    background: #1f6feb15; border: 1px solid #1f6feb44;
    font-size: 0.84rem; line-height: 1.6; min-height: 40px;
  }
  .preview b { font-variant-numeric: tabular-nums; }
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
    <button class="tab" data-panel="journal">יומן מסחר</button>
    <button class="tab" data-panel="news">חדשות מתפרצות</button>
    <button class="tab" data-panel="atr">חשיפת תנודתיות (ATR)</button>
    <button class="tab" data-panel="sector">פיזור סקטוריאלי</button>
  </div>

  <!-- ═══ Live monitoring (original view) ═══ -->
  <div id="panel-live" class="panel active">
    <div class="card">
      <div class="card-title">מניות במעקב</div>
      <div id="watchlist-chips" class="chips"><span class="volume">טוען…</span></div>
      <div class="inline-form">
        <input id="w-symbol" placeholder="טיקר חדש" maxlength="16" autocomplete="off">
        <button class="btn primary" onclick="addWatch()">+ הוספה למעקב</button>
        <span id="w-error" class="down" style="font-size:.78rem;align-self:center"></span>
      </div>
    </div>

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
        <button class="btn" onclick="exportExcel(this)" title="הורדת התיק ויומן המסחר כקובץ Excel">⬇️ הורדת אקסל</button>
        <span id="portfolio-total" class="volume" style="margin-inline-start:12px"></span>
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
        <div class="card-title">מדדים מול מניות בודדות</div>
        <div class="chart-box"><canvas id="indexChart"></canvas></div>
        <div style="padding:0 18px 16px">
          <div class="split-legend" id="split-legend"></div>
          <div id="index-detail" class="index-detail"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══ Trade journal ═══ -->
  <div id="panel-journal" class="panel">
    <div class="card">
      <div class="card-title">סיכום יומן המסחר</div>
      <div class="summary" id="journal-summary"><div class="volume">טוען…</div></div>
      <div class="card-actions" style="border-top:1px solid var(--border);border-bottom:none">
        <button class="btn" onclick="loadJournal()">רענון</button>
      </div>
    </div>
    <div id="journal-wrap" class="journal"></div>
  </div>

  <!-- ═══ Breaking news ═══ -->
  <div id="panel-news" class="panel">
    <div class="card">
      <div class="card-title">חדשות על מניות בתיק</div>
      <div class="card-actions">
        <button class="btn" onclick="loadNews(true)">סריקה מחדש</button>
        <span id="news-scanned" class="volume" style="margin-inline-start:12px"></span>
      </div>
      <div id="news-wrap"><div class="empty">טוען…</div></div>
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
    <label for="f-date">תאריך קנייה <span class="muted-hint">(אופציונלי — מחשב זמן החזקה)</span></label>
    <input id="f-date" type="date">
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

<!-- ═══ Add-to-position modal ═══ -->
<div class="modal-overlay" id="add-modal">
  <div class="modal">
    <h3 id="add-title">הוספה לפוזיציה</h3>
    <div class="volume" id="add-current" style="font-size:.8rem;margin-bottom:6px"></div>
    <label for="a-qty">כמה מניות קנית</label>
    <input id="a-qty" type="number" min="0.0001" step="any" placeholder="לדוגמה: 3">
    <label for="a-price">באיזה מחיר (למניה)</label>
    <input id="a-price" type="number" min="0.0001" step="any" placeholder="לדוגמה: 392.00">
    <label for="a-date">תאריך הקנייה <span class="muted-hint">(רק אם לא נרשם קודם)</span></label>
    <input id="a-date" type="date">
    <div class="preview" id="add-preview">—</div>
    <div class="form-error" id="a-error"></div>
    <div class="modal-actions">
      <button class="btn primary" onclick="confirmAdd()">הוספה</button>
      <button class="btn" onclick="closeAdd()">ביטול</button>
    </div>
  </div>
</div>

<!-- ═══ Sell modal ═══ -->
<div class="modal-overlay" id="sell-modal">
  <div class="modal">
    <h3 id="sell-title">מכירת פוזיציה</h3>
    <label>כמות למכירה <span class="muted-hint" id="sell-held"></span></label>
    <input id="s-qty" type="number" min="0.0001" step="any">
    <div style="margin-top:6px">
      <button class="btn" onclick="fillSell(1)">הכל</button>
      <button class="btn" onclick="fillSell(0.5)">50%</button>
      <button class="btn" onclick="fillSell(0.25)">25%</button>
    </div>
    <label for="s-price">מחיר מכירה (למניה)</label>
    <input id="s-price" type="number" min="0.0001" step="any">
    <label for="s-date">תאריך המכירה</label>
    <input id="s-date" type="date">
    <div class="form-error" id="s-error"></div>
    <div class="modal-actions">
      <button class="btn primary" onclick="confirmSell()">רישום מכירה</button>
      <button class="btn" onclick="closeSell()">ביטול</button>
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
  return `<bdi class="${cls}">${sign}${val.toFixed(2)}%</bdi>`;
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
/* Losses read as -$134.00, never $-134.00. */
const money = (n) => n == null ? '—' : '<bdi>' + (n < 0 ? '-$' : '$') +
  Math.abs(Number(n)).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) +
  '</bdi>';

/* The page is laid out LTR, so a Hebrew word after a number comes out reversed
   unless the run is explicitly marked. */
const days = (n) => n == null ? '—'
  : n === 0 ? 'היום'
  : `${n} ימים`;

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
let newsLoaded = false;

function showTab(name) {
  document.querySelectorAll('.tab').forEach((t) =>
    t.classList.toggle('active', t.dataset.panel === name));
  document.querySelectorAll('.panel').forEach((p) =>
    p.classList.toggle('active', p.id === 'panel-' + name));

  // Charts need a visible canvas to size correctly.
  if (name === 'portfolio' && sectorChart) {
    sectorChart.resize(); indexChart && indexChart.resize();
  }
  // The news scan hits yfinance once per holding, so it waits until the tab is
  // actually opened instead of slowing down every page load.
  if (name === 'news' && !newsLoaded) { newsLoaded = true; loadNews(false); }
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => showTab(tab.dataset.panel));
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
    document.getElementById('f-date').value = existing.purchase_date || '';
    // Only a manually set sector is pre-filled; an auto-detected one stays
    // blank so saving does not silently freeze today's yfinance answer.
    document.getElementById('f-sector').value = existing.sector_is_manual ? existing.sector : '';
    document.getElementById('f-asset-type').value = existing.asset_type_is_manual ? existing.asset_type : '';
  } else {
    document.getElementById('modal-title').textContent = 'הזנת פוזיציה';
    tickerEl.value = ''; tickerEl.disabled = false;
    document.getElementById('f-qty').value = '';
    document.getElementById('f-price').value = '';
    document.getElementById('f-date').value = '';
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
  const purchase_date = document.getElementById('f-date').value || null;
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
      body: JSON.stringify({ticker, quantity, entry_price, purchase_date, sector, asset_type}),
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
/* Held but unpriceable. Saying so beats a position quietly disappearing from
   the table, which reads as data loss. */
function skippedNote(data) {
  const skipped = data.skipped_tickers;
  if (!skipped || !skipped.length) return '';
  const reason = data.skip_reason ? ` (${esc(data.skip_reason)})` : '';
  // Every holding failing means the data source is refusing us, not that the
  // tickers are bad — say so, otherwise it reads as a portfolio problem.
  const all = !data.positions.length;
  const lead = all
    ? `⚠️ ספק הנתונים (Yahoo) לא מחזיר מחירים כרגע${reason}. ` +
      `הפוזיציות שמורות ולא אבדו — נסו שוב בעוד מספר דקות.`
    : `⚠️ לא ניתן לשלוף מחיר עבור ${skipped.map(esc).join(', ')}${reason} — ` +
      `הפוזיציות קיימות אך אינן נכללות בחישובים כרגע.`;
  return `<div class="banner warn" style="margin:14px 18px">${lead}</div>`;
}

function renderHoldings(data) {
  const wrap = document.getElementById('holdings-wrap');
  if (!data.positions.length) {
    wrap.innerHTML = skippedNote(data) ||
      '<div class="empty">אין פוזיציות — הוסיפו דרך "הוספת פוזיציה"</div>';
    document.getElementById('portfolio-total').textContent = '';
    return;
  }
  const pnlCls = data.total_pnl_value >= 0 ? 'up' : 'down';
  const live = data.positions.filter((p) => p.price_source === 'quote').length;
  const asOf = live === data.positions.length && live
    ? ` · <span class="volume">מחירים חיים</span>`
    : data.latest_bar_date
      ? ` · <span class="volume">מחירי סגירה מ-${esc(data.latest_bar_date)}</span>` : '';
  document.getElementById('portfolio-total').innerHTML =
    `שווי תיק: <b>${money(data.total_value)}</b> · ` +
    `רווח/הפסד כולל: <span class="${pnlCls}">${money(data.total_pnl_value)}</span>` + asOf;

  wrap.innerHTML = skippedNote(data) + `<table>
    <thead><tr>
      <th>סמל</th><th>כמות</th><th>מחיר כניסה</th><th>מחיר נוכחי</th>
      <th>פרי / פוסט</th><th>שווי</th><th>רווח/הפסד</th><th>ימי החזקה</th>
      <th>ATR%</th><th>סקטור</th><th></th>
    </tr></thead>
    <tbody>${data.positions.map((p) => `<tr>
      <td><span class="symbol">${esc(p.ticker)}</span></td>
      <td>${p.quantity}</td>
      <td><span class="price">${money(p.entry_price)}</span></td>
      <td><span class="price">${money(p.current_price)}</span>${priceDateCell(p)}</td>
      <td>${extendedCell(p)}</td>
      <td>${money(p.market_value)}</td>
      <td>${pctCell(p.pnl_pct)} <span class="volume">(${money(p.pnl_value)})</span></td>
      <td><span class="volume">${days(p.holding_days)}</span></td>
      <td><span class="volume">${p.atr_pct == null ? '—' : p.atr_pct.toFixed(2) + '%'}</span></td>
      <td>
        <span class="sector-cell ${p.sector === 'Unknown' ? 'unknown' : ''}"
              onclick="editHolding('${esc(p.ticker)}')"
              title="לחץ לעריכת הסקטור">${esc(p.sector)}${p.sector_is_manual ? ' ✎' : ''}</span>
      </td>
      <td class="row-actions">
        <button class="btn" onclick="analyzeTicker('${esc(p.ticker)}')" title="ניתוח AI של המניה">🧠</button>
        <button class="btn" onclick="openAdd('${esc(p.ticker)}')" title="קניית מניות נוספות">➕</button>
        <button class="btn" onclick="openSell('${esc(p.ticker)}')" title="רישום מכירה">💵</button>
        <button class="btn" onclick="editHolding('${esc(p.ticker)}')" title="עריכת הפוזיציה">✏️</button>
        <button class="btn" onclick="deleteHolding('${esc(p.ticker)}')" title="מחיקת הפוזיציה">🗑️</button>
      </td></tr>`).join('')}</tbody>
  </table>`;
}

/* Which session the close came from. A price with no date attached is taken
   for today's, which is how a stale quote goes unnoticed. */
function priceDateCell(p) {
  // The date wins over the source label. A quote whose last print is from an
  // earlier session carries that date; only a genuinely live one says so, or
  // an unchanging number reads as a live price and the tab looks frozen.
  if (!p.price_date) {
    return p.price_source === 'quote'
      ? `<div class="price-date up" title="מחיר מנקודת הציטוט החי של Yahoo">מחיר חי</div>`
      : '';
  }
  const cls = p.price_is_stale ? 'down' : 'volume';
  const mark = p.price_is_stale ? '⚠️ ' : '';
  return `<div class="price-date ${cls}" title="${p.price_is_stale
    ? 'מחיר מסשן קודם — לא עודכן בסשן האחרון' : 'סגירת המסחר האחרונה'}">` +
    `${mark}${esc(p.price_date)}</div>`;
}

/* Pre/post-market price. Outside those sessions the regular price already on
   the row is the whole story, so the cell stays empty rather than repeating it. */
function extendedCell(p) {
  if (p.session !== 'pre' && p.session !== 'after') return '<span class="flat">—</span>';
  const label = p.session === 'pre' ? 'Pre' : 'After';
  const price = p.extended_price == null ? '' : ' ' + money(p.extended_price);
  return `<span class="session-tag session-${p.session}">${label}</span>` +
         `<span class="ext-price">${price} ${pctCell(p.extended_change_pct)}</span>`;
}


function renderPie(canvasId, existing, entries, colors) {
  const ctx = document.getElementById(canvasId);
  if (existing) existing.destroy();
  if (!entries.length) return null;
  // Chart.js comes from a CDN. If it is blocked the tables must still render,
  // so a missing library degrades to "no chart" instead of throwing.
  if (typeof Chart === 'undefined') {
    ctx.style.display = 'none';
    const fallbackId = canvasId + '-fallback';
    if (!document.getElementById(fallbackId)) {
      ctx.insertAdjacentHTML('afterend',
        `<div id="${fallbackId}" class="empty">הגרפים לא נטענו (Chart.js לא זמין)</div>`);
    }
    return null;
  }
  return new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: entries.map((e) => e.label),
      datasets: [{
        data: entries.map((e) => e.weight_pct),
        backgroundColor: colors || CHART_COLORS,
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

/* Exactly two slices: money held through index funds / ETFs versus money in
   individual stocks. Index *membership* (which stock sits in which index) is
   listed as text below — as a pie it produced a dozen overlapping slices that
   obscured this split, which is the number that actually matters. */
const ETF_COLOR = '#bc8cff';
const STOCK_COLOR = '#58a6ff';

function renderAssetSplit(split, byIndex) {
  const legend = document.getElementById('split-legend');
  const detail = document.getElementById('index-detail');
  if (!split || (split.etf_pct === 0 && split.stock_pct === 0)) {
    legend.innerHTML = ''; detail.innerHTML = '';
    if (indexChart) { indexChart.destroy(); indexChart = null; }
    return;
  }

  indexChart = renderPie('indexChart', indexChart, [
    {label: 'מדדים / קרנות סל', weight_pct: split.etf_pct},
    {label: 'מניות בודדות',     weight_pct: split.stock_pct},
  ], [ETF_COLOR, STOCK_COLOR]);

  legend.innerHTML =
    `<span><i class="dot" style="background:${ETF_COLOR}"></i>מדדים / קרנות סל: ` +
    `<b>${split.etf_pct.toFixed(1)}%</b> <span class="volume">(${money(split.etf_value)})</span></span>` +
    `<span><i class="dot" style="background:${STOCK_COLOR}"></i>מניות בודדות: ` +
    `<b>${split.stock_pct.toFixed(1)}%</b> <span class="volume">(${money(split.stock_value)})</span></span>`;

  detail.innerHTML = (byIndex && byIndex.length)
    ? 'חשיפה למדדים: ' + byIndex.map((i) => `${esc(i.label)} ${i.weight_pct.toFixed(0)}%`).join(' · ')
    : '';
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
  let data;
  // Only the fetch is guarded. Folding the rendering into the same try meant a
  // chart failure reported itself as a data error and wiped the position table.
  try {
    data = await api('/api/portfolio');
  } catch (e) {
    document.getElementById('holdings-wrap').innerHTML =
      `<div class="empty" style="color:var(--red)">שגיאה: ${esc(e.message)}</div>`;
    return;
  }
  currentPositions = data.positions;
  renderHoldings(data);
  renderRisk(data);
  sectorChart = renderPie('sectorChart', sectorChart, data.allocation.by_sector);
  renderAssetSplit(data.allocation.by_asset_type, data.allocation.by_index);
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

/* Downloads the workbook. Built as a blob rather than a plain link so a failed
   request surfaces its error instead of navigating away to a broken page —
   and so the download inherits the page's auth headers. */
async function exportExcel(button) {
  // The button is passed in rather than read off the global `event`, which is
  // deprecated and undefined outside a direct handler call.
  const label = button ? button.textContent : '';
  if (button) { button.disabled = true; button.textContent = 'מכין…'; }
  try {
    const res = await fetch('/api/portfolio/export');
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = (res.headers.get('Content-Disposition') || '')
      .match(/filename="?([^"]+)"?/)?.[1] || 'portfolio.xlsx';
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('הורדת האקסל נכשלה: ' + e.message);
  } finally {
    if (button) { button.disabled = false; button.textContent = label; }
  }
}

function analyzeTicker(ticker) {
  return runAnalysis(`ניתוח AI — ${ticker}`, '/api/analyze/' + encodeURIComponent(ticker),
                     `מנתח את ${ticker}… (עשוי לקחת עד דקה)`);
}

/* ═══════════════════ Adding to a position ═══════════════════ */

let addTarget = null;

function openAdd(ticker) {
  const position = currentPositions.find((p) => p.ticker === ticker);
  if (!position) return;
  addTarget = position;
  document.getElementById('add-title').textContent = 'הוספה לפוזיציה — ' + ticker;
  document.getElementById('add-current').innerHTML =
    `מוחזק כעת: <b>${position.quantity}</b> מניות במחיר ממוצע <b>${money(position.entry_price)}</b>`;
  document.getElementById('a-qty').value = '';
  document.getElementById('a-price').value = position.current_price ?? '';
  // The date field only appears when the position has no recorded open date;
  // otherwise the original stands, because holding time runs from the first buy.
  const dateField = document.getElementById('a-date');
  const dateLabel = document.querySelector('#add-modal label[for="a-date"]');
  const showDate = !position.purchase_date;
  dateField.value = '';
  dateField.style.display = showDate ? 'block' : 'none';
  dateLabel.style.display = showDate ? 'block' : 'none';
  document.getElementById('a-error').textContent = '';
  updateAddPreview();
  document.getElementById('add-modal').classList.add('open');
}

function closeAdd() { document.getElementById('add-modal').classList.remove('open'); }

/* Shows the weighted average before it is committed — this dialog exists so
   the user does not have to do this sum, so it should show its work. */
function updateAddPreview() {
  const box = document.getElementById('add-preview');
  if (!addTarget) { box.textContent = '—'; return; }
  const qty = parseFloat(document.getElementById('a-qty').value);
  const price = parseFloat(document.getElementById('a-price').value);
  if (!Number.isFinite(qty) || qty <= 0 || !Number.isFinite(price) || price <= 0) {
    box.innerHTML = '<span class="volume">הזינו כמות ומחיר כדי לראות את הממוצע החדש</span>';
    return;
  }
  const total = addTarget.quantity + qty;
  const avg = (addTarget.quantity * addTarget.entry_price + qty * price) / total;
  const dir = avg > addTarget.entry_price ? 'down' : 'up';   // a higher basis is worse
  box.innerHTML =
    `כמות אחרי ההוספה: <b>${parseFloat(total.toFixed(6))}</b><br>` +
    `מחיר ממוצע חדש: <b class="${dir}">${money(avg)}</b> ` +
    `<span class="volume">(היה ${money(addTarget.entry_price)})</span><br>` +
    `<span class="volume">עלות ההוספה: ${money(qty * price)}</span>`;
}

['a-qty', 'a-price'].forEach((id) =>
  document.getElementById(id).addEventListener('input', updateAddPreview));

async function confirmAdd() {
  if (!addTarget) return;
  const quantity = parseFloat(document.getElementById('a-qty').value);
  const price = parseFloat(document.getElementById('a-price').value);
  const purchase_date = document.getElementById('a-date').value || null;
  const errEl = document.getElementById('a-error');
  errEl.textContent = '';
  if (!Number.isFinite(quantity) || quantity <= 0) { errEl.textContent = 'כמות חייבת להיות מספר חיובי'; return; }
  if (!Number.isFinite(price) || price <= 0) { errEl.textContent = 'מחיר חייב להיות מספר חיובי'; return; }
  try {
    await api('/api/holdings/' + encodeURIComponent(addTarget.ticker) + '/add', {
      method: 'POST',
      body: JSON.stringify({quantity, price, purchase_date}),
    });
    closeAdd();
    loadPortfolio();
  } catch (e) { errEl.textContent = e.message; }
}

/* ═══════════════════ Selling a position ═══════════════════ */

let sellTarget = null;

function openSell(ticker) {
  const position = currentPositions.find((p) => p.ticker === ticker);
  if (!position) return;
  sellTarget = position;
  document.getElementById('sell-title').textContent = 'מכירת פוזיציה — ' + ticker;
  document.getElementById('sell-held').textContent = `(מוחזק: ${position.quantity})`;
  document.getElementById('s-qty').value = position.quantity;
  // Defaulting to the live price makes the common case — "I just sold at
  // market" — a two-click operation, and it is still editable.
  document.getElementById('s-price').value = position.current_price ?? '';
  document.getElementById('s-date').value = new Date().toISOString().slice(0, 10);
  document.getElementById('s-error').textContent = '';
  document.getElementById('sell-modal').classList.add('open');
}

function closeSell() { document.getElementById('sell-modal').classList.remove('open'); }

function fillSell(fraction) {
  if (!sellTarget) return;
  const qty = sellTarget.quantity * fraction;
  // Trim floating-point noise from e.g. 0.1 * 3 without truncating real
  // fractional-share quantities.
  document.getElementById('s-qty').value = parseFloat(qty.toFixed(6));
}

async function confirmSell() {
  if (!sellTarget) return;
  const quantity = parseFloat(document.getElementById('s-qty').value);
  const exit_price = parseFloat(document.getElementById('s-price').value);
  const sold_date = document.getElementById('s-date').value || null;
  const errEl = document.getElementById('s-error');
  errEl.textContent = '';
  if (!Number.isFinite(quantity) || quantity <= 0) { errEl.textContent = 'כמות חייבת להיות מספר חיובי'; return; }
  if (quantity > sellTarget.quantity + 1e-9) { errEl.textContent = 'לא ניתן למכור יותר מהכמות המוחזקת'; return; }
  if (!Number.isFinite(exit_price) || exit_price <= 0) { errEl.textContent = 'מחיר מכירה חייב להיות מספר חיובי'; return; }
  try {
    await api('/api/holdings/' + encodeURIComponent(sellTarget.ticker) + '/sell', {
      method: 'POST',
      body: JSON.stringify({quantity, exit_price, sold_date}),
    });
    closeSell();
    loadPortfolio();
    loadJournal();
    showTab('journal');
  } catch (e) { errEl.textContent = e.message; }
}

/* ═══════════════════ Trade journal ═══════════════════ */

const RATING_LABEL = {green: 'החלטה טובה', orange: 'בינונית', red: 'טעונה שיפור'};

function renderJournal(data) {
  const s = data.summary;
  const pnlCls = s.total_pnl >= 0 ? 'up' : 'down';
  document.getElementById('journal-summary').innerHTML =
    `<div>עסקאות סגורות<b>${s.count}</b></div>` +
    `<div>רווח/הפסד מצטבר<b class="${pnlCls}">${money(s.total_pnl)}</b></div>` +
    `<div>אחוז עסקאות רווחיות<b>${s.win_rate_pct.toFixed(1)}%</b></div>` +
    `<div>זמן החזקה ממוצע<b>${days(s.avg_holding_days)}</b></div>`;

  const wrap = document.getElementById('journal-wrap');
  if (!data.entries.length) {
    wrap.innerHTML = '<div class="card"><div class="empty">' +
      'עדיין לא נרשמו מכירות — לחצו "מכירה" על פוזיציה בתיק כדי לפתוח את היומן</div></div>';
    return;
  }
  wrap.innerHTML = data.entries.map(renderEntry).join('');
}

function renderEntry(e) {
  const pnlCls = e.pnl_pct >= 0 ? 'up' : 'down';
  const lightCls = e.rating ? 'light-' + e.rating : 'light-none';
  const lightText = e.rating ? RATING_LABEL[e.rating] : 'טרם נותח';
  const partial = e.is_partial
    ? `<span class="badge">מכירה חלקית · ${(e.fraction_sold * 100).toFixed(0)}%</span>` : '';

  const analysis = e.ai_analysis
    ? `<div class="body">${esc(e.ai_analysis)}</div>`
    : `<div class="volume">טרם נותח. הניתוח נשמר במסד הנתונים ומורץ פעם אחת בלבד.</div>`;

  return `<div class="entry">
    <div class="entry-head">
      <span class="light ${lightCls}" title="${lightText}"></span>
      <span class="symbol">${esc(e.ticker)}</span>
      ${partial}
      <span class="grow volume">נמכר ב-${esc(e.sold_date)}</span>
      <button class="btn" onclick="analyzeEntry(${e.id})">
        ${e.ai_analysis ? '🧠 ניתוח מחדש' : '🧠 נתח עסקה'}
      </button>
    </div>

    <div class="entry-stats">
      <div><span>כמות</span><b>${e.quantity}</b></div>
      <!-- No arrow in the Hebrew label: arrows are bidi-mirrored, so the glyph
           flips against the reading flow. The values below are an isolated LTR
           run, where the arrow is unambiguous. -->
      <div><span>כניסה / יציאה</span><b>${money(e.entry_price)} → ${money(e.exit_price)}</b></div>
      <div><span>תשואה</span><b class="${pnlCls}">${e.pnl_pct >= 0 ? '+' : ''}${e.pnl_pct.toFixed(2)}%</b></div>
      <div><span>רווח/הפסד</span><b class="${pnlCls}">${money(e.pnl_value)}</b></div>
      <div><span>זמן החזקה</span><b>${days(e.holding_days)}</b></div>
      <div><span>ATR בעת המכירה</span><b>${e.atr_pct_at_close == null ? '—' : e.atr_pct_at_close.toFixed(2) + '%'}</b></div>
      <div><span>סקטור</span><b>${esc(e.sector || '—')}</b></div>
    </div>

    <div class="entry-section">
      <h4><span class="light ${lightCls}"></span> דירוג העסקה — ${lightText}</h4>
      <div id="entry-analysis-${e.id}">${analysis}</div>
    </div>

    <div class="entry-section">
      <h4>חוות דעת אישית</h4>
      <textarea class="note" id="note-${e.id}"
        placeholder="מה למדתי מהעסקה הזו?">${esc(e.personal_note || '')}</textarea>
      <div style="margin-top:8px">
        <button class="btn primary" onclick="saveNote(${e.id})">שמירה</button>
        <span class="save-hint" id="note-hint-${e.id}"></span>
      </div>
    </div>
  </div>`;
}

async function loadJournal() {
  try {
    renderJournal(await api('/api/journal'));
  } catch (e) {
    document.getElementById('journal-wrap').innerHTML =
      `<div class="card"><div class="empty" style="color:var(--red)">שגיאה: ${esc(e.message)}</div></div>`;
  }
}

async function analyzeEntry(id) {
  const box = document.getElementById('entry-analysis-' + id);
  box.innerHTML = '<div class="volume">מנתח את העסקה… (עשוי לקחת עד דקה)</div>';
  try {
    await api('/api/journal/' + id + '/analyze', {method: 'POST'});
    // Reload rather than patching in place: the rating light, the header
    // button and the analysis text all change together.
    loadJournal();
  } catch (e) {
    box.innerHTML = `<div class="down">שגיאה: ${esc(e.message)}</div>`;
  }
}

async function saveNote(id) {
  const hint = document.getElementById('note-hint-' + id);
  hint.style.color = ''; hint.textContent = 'שומר…';
  try {
    await api('/api/journal/' + id, {
      method: 'PATCH',
      body: JSON.stringify({personal_note: document.getElementById('note-' + id).value}),
    });
    hint.textContent = '✓ נשמר';
    setTimeout(() => { hint.textContent = ''; }, 2500);
  } catch (e) {
    hint.style.color = 'var(--red)';
    hint.textContent = 'שגיאה: ' + e.message;
  }
}

/* ═══════════════════ Breaking news ═══════════════════ */

function renderNews(data) {
  document.getElementById('news-scanned').textContent = data.scanned_at
    ? 'סריקה אחרונה: ' + new Date(data.scanned_at).toLocaleString('he-IL')
    : '';
  const wrap = document.getElementById('news-wrap');
  if (!data.items.length) {
    wrap.innerHTML = `<div class="empty">אין חדשות מה-${data.fresh_window_hours} שעות האחרונות ` +
                     `על המניות בתיק</div>`;
    return;
  }
  wrap.innerHTML = data.items.map((n) => {
    const age = n.age_hours == null ? ''
      : n.age_hours < 3
        ? `<span class="news-fresh">לפני ${n.age_hours.toFixed(1)} שעות</span>`
        : `לפני ${n.age_hours.toFixed(1)} שעות`;
    const title = n.link
      ? `<a href="${esc(n.link)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>`
      : esc(n.title);
    return `<div class="news-item">
      <span class="symbol">${esc(n.ticker)}</span> ${title}
      <div class="news-meta">${esc(n.publisher || '')} ${age ? '· ' + age : ''}</div>
    </div>`;
  }).join('');
}

async function loadNews(force) {
  if (force) document.getElementById('news-wrap').innerHTML = '<div class="empty">סורק…</div>';
  try {
    renderNews(await api('/api/news' + (force ? '?max_age_minutes=0' : '')));
  } catch (e) {
    document.getElementById('news-wrap').innerHTML =
      `<div class="empty" style="color:var(--red)">שגיאה: ${esc(e.message)}</div>`;
  }
}

/* ═══════════════════ Watchlist ═══════════════════ */

function renderWatchlist(symbols) {
  const wrap = document.getElementById('watchlist-chips');
  wrap.innerHTML = symbols.length
    ? symbols.map((s) => `<span class="chip">${esc(s)}` +
        `<button onclick="removeWatch('${esc(s)}')" title="הסרה ממעקב">×</button></span>`).join('')
    : '<span class="volume">אין מניות במעקב — הוסיפו טיקר למטה</span>';
}

async function loadWatchlist() {
  try {
    renderWatchlist((await api('/api/watchlist')).symbols);
  } catch (e) {
    document.getElementById('watchlist-chips').innerHTML =
      `<span class="down">שגיאה: ${esc(e.message)}</span>`;
  }
}

async function addWatch() {
  const input = document.getElementById('w-symbol');
  const errEl = document.getElementById('w-error');
  errEl.textContent = '';
  const symbol = input.value.trim().toUpperCase();
  if (!symbol) { errEl.textContent = 'יש להזין טיקר'; return; }
  try {
    renderWatchlist((await api('/api/watchlist', {
      method: 'POST', body: JSON.stringify({symbol}),
    })).symbols);
    input.value = '';
  } catch (e) { errEl.textContent = e.message; }
}

async function removeWatch(symbol) {
  if (!confirm('להסיר את ' + symbol + ' מהמעקב?')) return;
  try {
    renderWatchlist((await api('/api/watchlist/' + encodeURIComponent(symbol),
                               {method: 'DELETE'})).symbols);
  } catch (e) { document.getElementById('w-error').textContent = e.message; }
}

document.getElementById('w-symbol').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') addWatch();
});

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
document.getElementById('sell-modal').addEventListener('click', (e) => {
  if (e.target.id === 'sell-modal') closeSell();
});
document.getElementById('add-modal').addEventListener('click', (e) => {
  if (e.target.id === 'add-modal') closeAdd();
});

refresh();
loadBuild();
loadPortfolio();
loadWatchlist();
loadJournal();
setInterval(refresh, INTERVAL);
setInterval(loadPortfolio, 120000);   // portfolio prices refresh every 2 min
tickProgress();
</script>
</body>
</html>
"""
