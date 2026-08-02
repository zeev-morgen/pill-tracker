"""FastAPI application wiring the store, alert engine, AI analyst and dashboard.

Run with:  uvicorn main:app --reload

Optional security for public deployments: set DASHBOARD_USER and
DASHBOARD_PASSWORD environment variables to require HTTP Basic auth on
every route. When unset (local use), no login is required.
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from ai_analyst import AIAnalyst, AnalysisError
from alert_engine import AlertEngine
from dashboard import render_dashboard
from market_data import MarketDataError, MarketDataService
from store import Holding, PortfolioStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- auth

_basic = HTTPBasic(auto_error=False)


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(_basic)) -> None:
    expected_user = os.environ.get("DASHBOARD_USER")
    expected_password = os.environ.get("DASHBOARD_PASSWORD")
    if not expected_user or not expected_password:
        return  # auth disabled (local use)
    if (
        credentials is None
        or not secrets.compare_digest(credentials.username, expected_user)
        or not secrets.compare_digest(credentials.password, expected_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="נדרשת התחברות",
            headers={"WWW-Authenticate": "Basic"},
        )


app = FastAPI(title="Stock Tracker", version="2.1", dependencies=[Depends(require_auth)])

store = PortfolioStore()
market_data = MarketDataService()
alert_engine = AlertEngine(store, market_data)
analyst = AIAnalyst(market_data)


class PositionSummary(BaseModel):
    ticker: str
    quantity: float
    entry_price: float
    current_price: Optional[float]
    market_value: Optional[float]
    pnl_pct: Optional[float]
    pnl_value: Optional[float]
    sector: str


# ---------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return render_dashboard()


# ---------------------------------------------------------------- holdings

@app.get("/api/holdings", response_model=List[Holding])
def list_holdings() -> List[Holding]:
    return store.all()


@app.post("/api/holdings", response_model=Holding, status_code=201)
def upsert_holding(holding: Holding) -> Holding:
    # Pydantic already validated ticker format and positive quantity/price;
    # reject tickers yfinance doesn't recognize before persisting.
    try:
        market_data.fetch_history(holding.ticker, period="5d")
    except MarketDataError as exc:
        raise HTTPException(status_code=422, detail=f"טיקר לא מזוהה: {exc}") from exc
    return store.upsert(holding)


@app.delete("/api/holdings/{ticker}", status_code=204)
def delete_holding(ticker: str) -> None:
    if not store.delete(ticker):
        raise HTTPException(status_code=404, detail="הפוזיציה לא נמצאה")


# ---------------------------------------------------------------- portfolio

def _build_summary() -> List[PositionSummary]:
    positions: List[PositionSummary] = []
    for holding in store.all():
        current_price = market_value = pnl_pct = pnl_value = None
        sector = "Unknown"
        try:
            current_price = market_data.fetch_current_price(holding.ticker)
            market_value = current_price * holding.quantity
            sector = market_data.fetch_fundamentals(holding.ticker).sector
            pnl_pct = (current_price - holding.entry_price) / holding.entry_price * 100.0
            pnl_value = (current_price - holding.entry_price) * holding.quantity
        except MarketDataError as exc:
            logger.warning("summary: skipping prices for %s: %s", holding.ticker, exc)
        positions.append(
            PositionSummary(
                ticker=holding.ticker,
                quantity=holding.quantity,
                entry_price=holding.entry_price,
                current_price=current_price,
                market_value=market_value,
                pnl_pct=pnl_pct,
                pnl_value=pnl_value,
                sector=sector,
            )
        )
    return positions


@app.get("/api/portfolio/summary")
async def portfolio_summary() -> dict:
    positions = await run_in_threadpool(_build_summary)
    total = sum(p.market_value or 0.0 for p in positions)
    return {"total_value": round(total, 2), "positions": positions}


# ---------------------------------------------------------------- allocation

def _build_allocation() -> dict:
    by_sector: dict = {}
    by_index: dict = {}
    total = 0.0
    for holding in store.all():
        try:
            price = market_data.fetch_current_price(holding.ticker)
        except MarketDataError:
            continue
        value = price * holding.quantity
        total += value
        fundamentals = market_data.fetch_fundamentals(holding.ticker)
        by_sector[fundamentals.sector] = by_sector.get(fundamentals.sector, 0.0) + value
        # A stock in multiple indexes counts once per index (weights are
        # normalized per-index below, so slices still sum to 100%).
        for index_name in fundamentals.indexes:
            by_index[index_name] = by_index.get(index_name, 0.0) + value

    def to_weights(bucket: dict) -> list:
        bucket_total = sum(bucket.values())
        return [
            {"label": label, "weight_pct": round(v / bucket_total * 100.0, 2)}
            for label, v in sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)
        ] if bucket_total > 0 else []

    return {
        "total_value": round(total, 2),
        "by_sector": to_weights(by_sector),
        "by_index": to_weights(by_index),
    }


@app.get("/api/allocation")
async def allocation() -> dict:
    return await run_in_threadpool(_build_allocation)


# ---------------------------------------------------------------- alerts

@app.get("/api/alerts")
async def alerts() -> dict:
    volatility = await run_in_threadpool(alert_engine.volatility_exposure_report)
    sector = await run_in_threadpool(alert_engine.sector_diversification_report)
    return {"volatility": volatility, "sector": sector}


# ---------------------------------------------------------------- AI analysis

@app.get("/api/analyze/{ticker}")
async def analyze(ticker: str) -> dict:
    try:
        validated = Holding(ticker=ticker, quantity=1, entry_price=1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="טיקר לא תקין") from exc
    holding = store.get(validated.ticker)
    try:
        text = await run_in_threadpool(analyst.analyze, validated.ticker, holding)
    except AnalysisError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ticker": validated.ticker, "analysis": text}
