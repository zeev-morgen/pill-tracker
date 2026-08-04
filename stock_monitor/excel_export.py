"""Excel export of the portfolio and the trade journal.

Kept out of dashboard.py because it is a self-contained concern: it takes the
same report dictionaries the API already serves and turns them into a workbook.
That means it can be unit-tested without a server, and the sheet layout can
change without touching a route.

Numbers are written as numbers, not pre-formatted strings, so the file is
something you can pivot and total in Excel rather than only look at.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from io import BytesIO
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

#: Sheets are right-to-left, matching the dashboard and the Hebrew headers.
_RTL = True

_MONEY = '#,##0.00'
_PERCENT = '0.00"%"'          # values are already percentages, not fractions
_DATE = "yyyy-mm-dd"

_RATING_LABELS = {"green": "ירוק — החלטה טובה",
                  "orange": "כתום — בינונית",
                  "red": "אדום — טעונה שיפור"}

_SESSION_LABELS = {"pre": "פרי-מרקט", "regular": "מסחר רגיל",
                   "after": "אפטר-מרקט", "closed": "סגור"}


def _positions_frame(report: dict) -> pd.DataFrame:
    rows = []
    for p in report.get("positions", []):
        rows.append({
            "סמל": p.get("ticker"),
            "כמות": p.get("quantity"),
            "מחיר כניסה": p.get("entry_price"),
            "מחיר נוכחי": p.get("current_price"),
            # Without this the sheet cannot tell a live quote from a close that
            # happens to be several sessions old.
            "מקור המחיר": "ציטוט חי" if p.get("price_source") == "quote" else "סגירה",
            "נכון לתאריך": _as_date(p.get("price_date")),
            "מחיר ישן?": "כן" if p.get("price_is_stale") else "",
            "שווי שוק": p.get("market_value"),
            "רווח/הפסד ($)": p.get("pnl_value"),
            "רווח/הפסד (%)": p.get("pnl_pct"),
            "ATR (%)": p.get("atr_pct"),
            "סקטור": p.get("sector"),
            "סוג נכס": "קרן סל" if p.get("asset_type") == "etf" else "מניה",
            "תאריך קנייה": _as_date(p.get("purchase_date")),
            "ימי החזקה": p.get("holding_days"),
        })
    return pd.DataFrame(rows)


def _journal_frame(entries: List[dict]) -> pd.DataFrame:
    rows = []
    for e in entries:
        rows.append({
            "סמל": e.get("ticker"),
            "כמות שנמכרה": e.get("quantity"),
            "מחיר כניסה": e.get("entry_price"),
            "מחיר יציאה": e.get("exit_price"),
            "תאריך קנייה": _as_date(e.get("purchase_date")),
            "תאריך מכירה": _as_date(e.get("sold_date")),
            "ימי החזקה": e.get("holding_days"),
            "רווח/הפסד ($)": e.get("pnl_value"),
            "רווח/הפסד (%)": e.get("pnl_pct"),
            "מכירה חלקית": (
                f"{e['fraction_sold'] * 100:.0f}%" if e.get("is_partial") else ""
            ),
            "ATR בעת המכירה (%)": e.get("atr_pct_at_close"),
            "סקטור": e.get("sector"),
            "דירוג": _RATING_LABELS.get(e.get("rating") or "", ""),
            "ניתוח AI": e.get("ai_analysis") or "",
            "חוות דעת אישית": e.get("personal_note") or "",
        })
    return pd.DataFrame(rows)


def _summary_frame(report: dict, journal: dict) -> pd.DataFrame:
    volatility = report.get("volatility") or {}
    sector = report.get("sector") or {}
    allocation = (report.get("allocation") or {}).get("by_asset_type") or {}
    summary = journal.get("summary") or {}

    pairs = [
        ("נוצר בתאריך", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("מספר פוזיציות", len(report.get("positions") or [])),
        ("שווי תיק כולל", report.get("total_value")),
        ("רווח/הפסד לא ממומש", report.get("total_pnl_value")),
        ("", ""),
        ("חשיפה לתנודתיות גבוהה (%)", volatility.get("exposure_pct")),
        ("סף התראה לתנודתיות (%)", volatility.get("threshold_pct")),
        ("סף ריכוזיות סקטוריאלית (%)", sector.get("threshold_pct")),
        ("במדדים / קרנות סל (%)", allocation.get("etf_pct")),
        ("במניות בודדות (%)", allocation.get("stock_pct")),
        ("", ""),
        ("עסקאות סגורות", summary.get("count")),
        ("רווח/הפסד ממומש", summary.get("total_pnl")),
        ("אחוז עסקאות רווחיות", summary.get("win_rate_pct")),
        ("זמן החזקה ממוצע (ימים)", summary.get("avg_holding_days")),
    ]
    skipped = report.get("skipped_tickers") or []
    if skipped:
        # Otherwise the totals silently exclude them and the file looks complete.
        pairs += [("", ""), ("מניות שלא ניתן היה לתמחר", ", ".join(skipped))]

    return pd.DataFrame(pairs, columns=["נתון", "ערך"])


def _sector_frame(report: dict) -> pd.DataFrame:
    rows = [
        {"סקטור": s.get("sector"),
         "שווי": s.get("market_value"),
         "משקל בתיק (%)": s.get("weight_pct")}
        for s in (report.get("sector") or {}).get("sectors", [])
    ]
    return pd.DataFrame(rows)


def _as_date(value) -> Optional[date]:
    """ISO strings become real dates so Excel can sort and filter them."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


#: Column header -> Excel number format. Applied by header text, so a column
#: keeps its format wherever it sits and whichever sheet it is on.
_FORMATS = {
    "כמות": '#,##0.####',
    "כמות שנמכרה": '#,##0.####',
    "מחיר כניסה": _MONEY,
    "מחיר יציאה": _MONEY,
    "מחיר נוכחי": _MONEY,
    "שווי שוק": _MONEY,
    "שווי": _MONEY,
    "רווח/הפסד ($)": _MONEY,
    "רווח/הפסד (%)": _PERCENT,
    "ATR (%)": _PERCENT,
    "ATR בעת המכירה (%)": _PERCENT,
    "משקל בתיק (%)": _PERCENT,
    "נכון לתאריך": _DATE,
    "תאריך קנייה": _DATE,
    "תאריך מכירה": _DATE,
}

_WIDTHS = {"ניתוח AI": 60, "חוות דעת אישית": 40, "דירוג": 20,
           "נתון": 30, "ערך": 26, "סקטור": 22}


def _style(worksheet, frame: pd.DataFrame) -> None:
    """Freeze the header, size the columns and format the numbers."""
    from openpyxl.styles import Alignment, Font, PatternFill

    worksheet.sheet_view.rightToLeft = _RTL
    if frame.empty:
        return

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F6FEB")
    for cell in worksheet[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for index, column in enumerate(frame.columns, start=1):
        letter = worksheet.cell(row=1, column=index).column_letter
        if column in _WIDTHS:
            width = _WIDTHS[column]
        else:
            longest = max(
                [len(str(column))] +
                [len(str(v)) for v in frame[column].head(200) if v is not None]
            )
            width = min(max(longest + 3, 10), 32)
        worksheet.column_dimensions[letter].width = width

        number_format = _FORMATS.get(column)
        wrap = column in ("ניתוח AI", "חוות דעת אישית")
        if not number_format and not wrap:
            continue
        for row in range(2, worksheet.max_row + 1):
            cell = worksheet.cell(row=row, column=index)
            if number_format:
                cell.number_format = number_format
            if wrap:
                cell.alignment = Alignment(wrap_text=True, vertical="top")


def build_workbook(report: dict, journal: dict) -> bytes:
    """Render the portfolio and journal as a .xlsx file.

    Takes the same dictionaries the API serves, so the numbers in the sheet are
    by construction the numbers on the screen.
    """
    sheets = {
        "סיכום": _summary_frame(report, journal),
        "פוזיציות": _positions_frame(report),
        "יומן מסחר": _journal_frame(journal.get("entries") or []),
        "סקטורים": _sector_frame(report),
    }

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            # An empty sheet still gets written, with its headers: a missing tab
            # reads as a broken export rather than an empty journal.
            if frame.empty and name in ("פוזיציות", "יומן מסחר", "סקטורים"):
                frame = pd.DataFrame(columns=frame.columns)
            frame.to_excel(writer, sheet_name=name, index=False)
            _style(writer.sheets[name], frame)

    return buffer.getvalue()


def filename(today: Optional[date] = None) -> str:
    return f"portfolio-{(today or date.today()).isoformat()}.xlsx"
