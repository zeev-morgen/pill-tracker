"""AI analyst: personalized stock recommendations via the Claude API.

``_build_prompt`` injects the user's personal position (quantity, purchase
date, entry price, actual P/L) alongside current technicals, so the model
compares the position's performance since entry against today's chart and
tailors its recommendation to the user's entry point.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import anthropic

from alert_engine import compute_atr
from market_data import MarketDataError, MarketDataService
from store import Holding

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")


class AnalysisError(Exception):
    """Raised when an AI analysis cannot be produced."""


@dataclass
class PositionContext:
    """Everything the prompt needs about the user's personal position."""

    quantity: float
    entry_price: float
    current_price: float

    @property
    def pnl_pct(self) -> float:
        return (self.current_price - self.entry_price) / self.entry_price * 100.0

    @property
    def pnl_value(self) -> float:
        return (self.current_price - self.entry_price) * self.quantity


class AIAnalyst:
    def __init__(
        self,
        market_data: MarketDataService,
        model: str = DEFAULT_MODEL,
        client: Optional[anthropic.Anthropic] = None,
    ):
        self._market = market_data
        self._model = model
        # Injectable client keeps this class testable without network access.
        # Built lazily so the server starts even when no API key is configured.
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            try:
                self._client = anthropic.Anthropic()
            except anthropic.AnthropicError as exc:
                raise AnalysisError(
                    "AI client not configured — set ANTHROPIC_API_KEY"
                ) from exc
        return self._client

    # -- prompt construction -------------------------------------------------

    def _build_prompt(
        self,
        ticker: str,
        technical_summary: str,
        fundamentals_summary: str,
        position: Optional[PositionContext],
    ) -> str:
        sections = [
            f"You are a professional equity analyst. Analyze the stock {ticker}.",
            f"## Current technical picture\n{technical_summary}",
            f"## Fundamentals\n{fundamentals_summary}",
        ]
        if position is not None:
            sections.append(
                "## The user's personal position\n"
                f"- Quantity held: {position.quantity}\n"
                f"- Entry price (user-entered): {position.entry_price:.2f}\n"
                f"- Current price: {position.current_price:.2f}\n"
                f"- Actual P/L since entry: {position.pnl_pct:+.2f}% "
                f"({position.pnl_value:+,.2f} in currency terms)\n"
            )
            sections.append(
                "Compare the position's actual performance since the entry price "
                "against today's technical picture, and give a recommendation "
                "(buy more / hold / trim / exit) tailored to THIS entry point — "
                "not a generic rating. Address whether the original entry thesis "
                "still holds and what price levels should trigger a decision."
            )
        else:
            sections.append(
                "The user holds no position in this stock. Provide a general "
                "technical and fundamental assessment with clear entry levels."
            )
        sections.append(
            "Answer in Hebrew. Be concise and structured: a one-line verdict "
            "first, then supporting reasoning. This is decision support, not "
            "financial advice — note that briefly at the end."
        )
        return "\n\n".join(sections)

    # -- data assembly -------------------------------------------------------

    def _technical_summary(self, ticker: str) -> str:
        df = self._market.fetch_history(ticker, period="6mo")
        close = df["Close"]
        price = float(close.iloc[-1])
        sma20 = float(close.rolling(20).mean().iloc[-1])
        sma50 = float(close.rolling(50).mean().iloc[-1])
        high_6m, low_6m = float(close.max()), float(close.min())
        change_1m = (
            (price / float(close.iloc[-21]) - 1.0) * 100.0 if len(close) > 21 else 0.0
        )
        atr = compute_atr(df)
        atr_line = (
            f"ATR(14): {atr:.2f} ({atr / price * 100.0:.2f}% of price)" if atr else "ATR: n/a"
        )
        return (
            f"Price: {price:.2f} | SMA20: {sma20:.2f} | SMA50: {sma50:.2f}\n"
            f"6M range: {low_6m:.2f}–{high_6m:.2f} | 1M change: {change_1m:+.2f}%\n"
            f"{atr_line}"
        )

    def _position_context(
        self, ticker: str, holding: Optional[Holding]
    ) -> Optional[PositionContext]:
        if holding is None:
            return None
        current = self._market.fetch_current_price(ticker)
        return PositionContext(
            quantity=holding.quantity,
            entry_price=holding.entry_price,
            current_price=current,
        )

    # -- public API ----------------------------------------------------------

    def analyze(self, ticker: str, holding: Optional[Holding]) -> str:
        try:
            technical = self._technical_summary(ticker)
            fundamentals = self._market.fetch_fundamentals(ticker)
            position = self._position_context(ticker, holding)
        except MarketDataError as exc:
            raise AnalysisError(str(exc)) from exc

        fundamentals_summary = (
            f"{fundamentals.name} | Sector: {fundamentals.sector} | "
            f"Industry: {fundamentals.industry} | "
            f"Indexes: {', '.join(fundamentals.indexes)}"
        )
        prompt = self._build_prompt(ticker, technical, fundamentals_summary, position)

        try:
            response = self.client.messages.create(
                model=self._model,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.RateLimitError as exc:
            raise AnalysisError("AI service rate limited — try again shortly") from exc
        except anthropic.APIStatusError as exc:
            raise AnalysisError(f"AI service error ({exc.status_code})") from exc
        except anthropic.APIConnectionError as exc:
            raise AnalysisError("could not reach the AI service") from exc

        if response.stop_reason == "refusal":
            raise AnalysisError("the AI declined to analyze this request")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise AnalysisError("empty response from the AI service")
        return text
