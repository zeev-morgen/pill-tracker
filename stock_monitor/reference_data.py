"""
Curated ticker reference data: sector classification and ETF identification.

Why this exists: sectors are meant to come from yfinance's ``.info``, but that
endpoint frequently returns an empty payload (throttling, upstream changes), so
in practice every holding fell back to "Unknown" and the sector breakdown was a
single meaningless slice.

This table is consulted whenever yfinance gives nothing, so a portfolio of
common tickers is classified correctly out of the box and the manual override
is only needed for what genuinely is not covered here.

Sector names follow Yahoo Finance's vocabulary so both sources agree:
Technology, Healthcare, Financial Services, Consumer Cyclical,
Consumer Defensive, Energy, Basic Materials, Industrials, Utilities,
Real Estate, Communication Services.

Only entries verified against the company's actual classification belong here —
a wrong sector is worse than "Unknown", because the user has no reason to go
and correct it.
"""

from __future__ import annotations

from typing import Dict, Optional, Set

#: Broad-market and index-tracking funds. "Diversified" marks them as spanning
#: sectors rather than belonging to one.
DIVERSIFIED = "Diversified"

#: Funds, not companies — these drive the index-vs-single-stock split.
KNOWN_ETFS: Set[str] = {
    # US broad market / index trackers
    "SPY", "VOO", "IVV", "SPLG", "VTI", "ITOT", "SCHB",
    "QQQ", "QQQM", "DIA", "IWM", "IWB", "IWV", "VTV", "VUG", "VO", "VB",
    "RSP", "MDY", "SCHX", "SCHG", "SCHA",
    # Dividend / factor
    "SCHD", "VYM", "VIG", "DGRO", "NOBL", "HDV", "SPHD", "USMV", "QUAL", "MTUM",
    # International / global
    "VT", "VXUS", "VEA", "VWO", "IEFA", "IEMG", "EFA", "EEM", "ACWI", "IXUS",
    # Bonds
    "BND", "AGG", "BNDX", "TLT", "IEF", "SHY", "LQD", "HYG", "TIP", "VCIT", "VCSH",
    # Sector SPDRs and popular thematics
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    "VGT", "VHT", "VFH", "VDE", "VNQ", "SMH", "SOXX", "IBB", "XBI", "ARKK",
    "TAN", "ICLN", "IYR", "GDX", "JETS", "XOP", "KRE",
    # Commodities
    "GLD", "IAU", "SLV", "USO", "DBC", "PDBC",
}

#: Ticker → sector. Verified classifications only.
SECTOR_BY_TICKER: Dict[str, str] = {
    # ── Technology ───────────────────────────────────────────────────────────
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AVGO": "Technology", "ORCL": "Technology", "CRM": "Technology",
    "AMD": "Technology", "INTC": "Technology", "ADBE": "Technology",
    "CSCO": "Technology", "ACN": "Technology", "IBM": "Technology",
    "QCOM": "Technology", "TXN": "Technology", "AMAT": "Technology",
    "LRCX": "Technology", "KLAC": "Technology", "MU": "Technology",
    "ADI": "Technology", "NXPI": "Technology", "MRVL": "Technology",
    "SNPS": "Technology", "CDNS": "Technology", "PANW": "Technology",
    "NOW": "Technology", "INTU": "Technology", "ANET": "Technology",
    "DELL": "Technology", "SMCI": "Technology", "HPQ": "Technology",
    "HPE": "Technology", "WDC": "Technology", "STX": "Technology",
    "SNDK": "Technology", "TSM": "Technology", "ASML": "Technology",
    "ARM": "Technology", "CRWD": "Technology", "FTNT": "Technology",
    "DDOG": "Technology", "SNOW": "Technology", "MDB": "Technology",
    "TEAM": "Technology", "WDAY": "Technology", "ZS": "Technology",
    "SEDG": "Technology", "ENPH": "Technology", "FSLR": "Technology",
    "ON": "Technology", "MCHP": "Technology", "GLW": "Technology",
    "APH": "Technology", "TER": "Technology", "SWKS": "Technology",

    # ── Communication Services ───────────────────────────────────────────────
    "GOOGL": "Communication Services", "GOOG": "Communication Services",
    "META": "Communication Services", "NFLX": "Communication Services",
    "DIS": "Communication Services", "CMCSA": "Communication Services",
    "T": "Communication Services", "VZ": "Communication Services",
    "TMUS": "Communication Services", "CHTR": "Communication Services",
    "EA": "Communication Services", "TTWO": "Communication Services",
    "WBD": "Communication Services", "SPOT": "Communication Services",
    "RBLX": "Communication Services", "PINS": "Communication Services",
    "SNAP": "Communication Services", "OMC": "Communication Services",

    # ── Consumer Cyclical ────────────────────────────────────────────────────
    "AMZN": "Consumer Cyclical", "TSLA": "Consumer Cyclical",
    "HD": "Consumer Cyclical", "MCD": "Consumer Cyclical",
    "NKE": "Consumer Cyclical", "SBUX": "Consumer Cyclical",
    "LOW": "Consumer Cyclical", "BKNG": "Consumer Cyclical",
    "ABNB": "Consumer Cyclical", "TJX": "Consumer Cyclical",
    "F": "Consumer Cyclical", "GM": "Consumer Cyclical",
    "RIVN": "Consumer Cyclical", "LCID": "Consumer Cyclical",
    "MAR": "Consumer Cyclical", "HLT": "Consumer Cyclical",
    "CMG": "Consumer Cyclical", "ORLY": "Consumer Cyclical",
    "AZO": "Consumer Cyclical", "ROST": "Consumer Cyclical",
    "LULU": "Consumer Cyclical", "DHI": "Consumer Cyclical",
    "LEN": "Consumer Cyclical", "EBAY": "Consumer Cyclical",

    # ── Consumer Defensive ───────────────────────────────────────────────────
    "WMT": "Consumer Defensive", "COST": "Consumer Defensive",
    "PG": "Consumer Defensive", "KO": "Consumer Defensive",
    "PEP": "Consumer Defensive", "PM": "Consumer Defensive",
    "MO": "Consumer Defensive", "MDLZ": "Consumer Defensive",
    "CL": "Consumer Defensive", "TGT": "Consumer Defensive",
    "KMB": "Consumer Defensive", "GIS": "Consumer Defensive",
    "KHC": "Consumer Defensive", "STZ": "Consumer Defensive",
    "SYY": "Consumer Defensive", "KR": "Consumer Defensive",
    "HSY": "Consumer Defensive", "DG": "Consumer Defensive",

    # ── Healthcare ───────────────────────────────────────────────────────────
    "LLY": "Healthcare", "UNH": "Healthcare", "JNJ": "Healthcare",
    "ABBV": "Healthcare", "MRK": "Healthcare", "TMO": "Healthcare",
    "ABT": "Healthcare", "PFE": "Healthcare", "DHR": "Healthcare",
    "AMGN": "Healthcare", "BMY": "Healthcare", "GILD": "Healthcare",
    "ISRG": "Healthcare", "VRTX": "Healthcare", "REGN": "Healthcare",
    "MDT": "Healthcare", "SYK": "Healthcare", "BSX": "Healthcare",
    "ELV": "Healthcare", "CI": "Healthcare", "CVS": "Healthcare",
    "HCA": "Healthcare", "ZTS": "Healthcare", "MRNA": "Healthcare",
    "BIIB": "Healthcare", "ABCL": "Healthcare", "FULC": "Healthcare",
    "EW": "Healthcare", "IDXX": "Healthcare", "IQV": "Healthcare",
    "A": "Healthcare", "MCK": "Healthcare", "COR": "Healthcare",

    # ── Financial Services ───────────────────────────────────────────────────
    "BRK-B": "Financial Services", "BRK.B": "Financial Services",
    "JPM": "Financial Services", "V": "Financial Services",
    "MA": "Financial Services", "BAC": "Financial Services",
    "WFC": "Financial Services", "GS": "Financial Services",
    "MS": "Financial Services", "SCHW": "Financial Services",
    "AXP": "Financial Services", "C": "Financial Services",
    "BLK": "Financial Services", "SPGI": "Financial Services",
    "CB": "Financial Services", "PGR": "Financial Services",
    "MMC": "Financial Services", "PYPL": "Financial Services",
    "COF": "Financial Services", "USB": "Financial Services",
    "PNC": "Financial Services", "TFC": "Financial Services",
    "AON": "Financial Services", "ICE": "Financial Services",
    "CME": "Financial Services", "MCO": "Financial Services",
    "COIN": "Financial Services", "HOOD": "Financial Services",
    "AIG": "Financial Services", "MET": "Financial Services",
    "PRU": "Financial Services", "ALL": "Financial Services",

    # ── Energy ───────────────────────────────────────────────────────────────
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "SLB": "Energy",
    "EOG": "Energy", "MPC": "Energy", "PSX": "Energy", "VLO": "Energy",
    "OXY": "Energy", "WMB": "Energy", "KMI": "Energy", "HAL": "Energy",
    "DVN": "Energy", "HES": "Energy", "BKR": "Energy", "OKE": "Energy",
    "FANG": "Energy", "TRGP": "Energy",

    # ── Basic Materials ──────────────────────────────────────────────────────
    "LIN": "Basic Materials", "APD": "Basic Materials", "SHW": "Basic Materials",
    "ECL": "Basic Materials", "NEM": "Basic Materials", "FCX": "Basic Materials",
    "DOW": "Basic Materials", "DD": "Basic Materials", "CF": "Basic Materials",
    "NUE": "Basic Materials", "VMC": "Basic Materials", "MLM": "Basic Materials",
    "PPG": "Basic Materials", "ALB": "Basic Materials", "MOS": "Basic Materials",
    "IFF": "Basic Materials", "STLD": "Basic Materials",

    # ── Industrials ──────────────────────────────────────────────────────────
    "GE": "Industrials", "CAT": "Industrials", "RTX": "Industrials",
    "HON": "Industrials", "UNP": "Industrials", "BA": "Industrials",
    "LMT": "Industrials", "DE": "Industrials", "UPS": "Industrials",
    "ADP": "Industrials", "ETN": "Industrials", "EMR": "Industrials",
    "NOC": "Industrials", "GD": "Industrials", "MMM": "Industrials",
    "FDX": "Industrials", "CSX": "Industrials", "NSC": "Industrials",
    "WM": "Industrials", "ITW": "Industrials", "PH": "Industrials",
    "TDG": "Industrials", "CARR": "Industrials", "JCI": "Industrials",
    "CMI": "Industrials", "PCAR": "Industrials", "URI": "Industrials",
    "PWR": "Industrials", "DAL": "Industrials", "UAL": "Industrials",

    # ── Utilities ────────────────────────────────────────────────────────────
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    "D": "Utilities", "AEP": "Utilities", "EXC": "Utilities",
    "SRE": "Utilities", "XEL": "Utilities", "ED": "Utilities",
    "PEG": "Utilities", "WEC": "Utilities", "VST": "Utilities",
    "CEG": "Utilities", "PCG": "Utilities", "EIX": "Utilities",

    # ── Real Estate ──────────────────────────────────────────────────────────
    "PLD": "Real Estate", "AMT": "Real Estate", "EQIX": "Real Estate",
    "CCI": "Real Estate", "PSA": "Real Estate", "SPG": "Real Estate",
    "O": "Real Estate", "WELL": "Real Estate", "DLR": "Real Estate",
    "AVB": "Real Estate", "EQR": "Real Estate", "VICI": "Real Estate",
    "CBRE": "Real Estate", "IRM": "Real Estate",
}


def lookup_sector(ticker: str) -> Optional[str]:
    """Curated sector for a ticker, or None when it is not covered.

    ETFs report ``DIVERSIFIED``: they span sectors, so filing them under one
    would distort the concentration report.
    """
    symbol = ticker.strip().upper()
    if symbol in KNOWN_ETFS:
        return DIVERSIFIED
    return SECTOR_BY_TICKER.get(symbol)


def is_known_etf(ticker: str) -> bool:
    return ticker.strip().upper() in KNOWN_ETFS
