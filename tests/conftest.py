"""Shared test setup.

Keeps the suite off the network. ``PortfolioRiskAnalyzer`` batches its price
history through ``yf.download``; left unstubbed every test that builds a report
tries to reach Yahoo, which is both slow and dependent on the machine running
the tests. Returning an empty frame means "the batch covered nothing", so each
test falls through to whatever ``yf.Ticker`` stub it installed — exercising the
per-ticker fallback path rather than skipping the fetch entirely.
"""

import pandas as pd
import pytest

from stock_monitor import dashboard as _dashboard
from stock_monitor import portfolio_risk


@pytest.fixture(autouse=True)
def no_batch_download(monkeypatch):
    monkeypatch.setattr(
        portfolio_risk.yf, "download", lambda *args, **kwargs: pd.DataFrame()
    )


#: Captured at import, before the autouse fixture below replaces it, so tests
#: that exercise the verifier itself can still reach the real implementation.
_REAL_SYMBOL_IS_KNOWN = _dashboard.symbol_is_known


@pytest.fixture(autouse=True)
def no_symbol_verification(monkeypatch):
    """Adding a watchlist symbol asks Yahoo whether it exists; here nobody asks.

    None is the "could not tell" answer, which is what the route sees in
    production when Yahoo is unreachable — the symbol is accepted on trust.
    Tests that care about the verified or rejected paths override this.
    """
    monkeypatch.setattr(_dashboard, "symbol_is_known", lambda symbol: None)


@pytest.fixture
def real_symbol_check(monkeypatch):
    """Puts the genuine verifier back, for tests aimed at it rather than past it."""
    monkeypatch.setattr(_dashboard, "symbol_is_known", _REAL_SYMBOL_IS_KNOWN)
    return _REAL_SYMBOL_IS_KNOWN
