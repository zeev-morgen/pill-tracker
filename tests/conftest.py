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

from stock_monitor import portfolio_risk


@pytest.fixture(autouse=True)
def no_batch_download(monkeypatch):
    monkeypatch.setattr(
        portfolio_risk.yf, "download", lambda *args, **kwargs: pd.DataFrame()
    )
