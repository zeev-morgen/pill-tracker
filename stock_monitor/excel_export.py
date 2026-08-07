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
    """The holdings table as it appears on screen, same columns, same order.

    The point of the export is to be that table in a spreadsheet, so the layout
    follows the dashboard rather than inventing its own. Two columns the screen
    shows visually rather than as text are spelled out here — where the price
    came from, and the pre/post-market move — because a file outlives the
    session it was exported from.
    """
    rows = []
    for p in report.get("positions", []):
        source = "ציטוט חי" if p.get("price_source") == "quote" else "סגירה"
        if p.get("price_is_stale"):
            source = "סגירה (מסשן קודם)"
        rows.append({
            "סמל": p.get("ticker"),
            "כמות": p.get("quantity"),
            "מחיר כניסה": p.get("entry_price"),
            "מחיר נוכחי": p.get("current_price"),
            # Without this column the two price columns above are unreadable:
            # a Tel Aviv price of 3,450 is agorot, not dollars, and nothing in
            # the file would say so once it is off the screen.
            "מטבע": _currency_label(p.get("currency")),
            "נכון לתאריך": _as_date(p.get("price_date")),
            "מקור המחיר": source,
            "פרי / פוסט": p.get("extended_price"),
            "שינוי פרי / פוסט (%)": p.get("extended_change_pct"),
            "שווי ($)": p.get("market_value"),
            "רווח/הפסד ($)": p.get("pnl_value"),
            "רווח/הפסד (%)": p.get("pnl_pct"),
            "ימי החזקה": p.get("holding_days"),
            "ATR (%)": p.get("atr_pct"),
            "סקטור": p.get("sector"),
            "תאריך קנייה": _as_date(p.get("purchase_date")),
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


def _currency_label(code) -> str:
    """The unit a price column's numbers are in, spelled out.

    Agorot rather than "₪": the difference is a factor of a hundred, and a
    spreadsheet outlives the context that would have made it obvious.
    """
    code = str(code or "").strip().upper()
    return {"ILA": "אגורות", "ILS": "שקל"}.get(code, "דולר")


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
    "שווי ($)": _MONEY,
    "פרי / פוסט": _MONEY,
    "שינוי פרי / פוסט (%)": _PERCENT,
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


def _append_totals(worksheet, frame: pd.DataFrame, report: dict) -> None:
    """A totals row under the table, plus a note for anything left out.

    Mirrors the line above the table on screen ("שווי תיק … רווח/הפסד כולל").
    The totals are real SUM formulas, not baked numbers, so they still add up
    if rows are filtered or edited in Excel.
    """
    from openpyxl.styles import Border, Font, Side

    if frame.empty:
        return

    last = worksheet.max_row
    total_row = last + 1
    columns = list(frame.columns)

    label_cell = worksheet.cell(row=total_row, column=1, value='סה"כ')
    label_cell.font = Font(bold=True)
    top = Border(top=Side(style="double"))
    for index in range(1, len(columns) + 1):
        worksheet.cell(row=total_row, column=index).border = top

    for column in ("שווי ($)", "רווח/הפסד ($)"):
        if column not in columns:
            continue
        index = columns.index(column) + 1
        letter = worksheet.cell(row=1, column=index).column_letter
        cell = worksheet.cell(row=total_row, column=index)
        cell.value = f"=SUM({letter}2:{letter}{last})"
        cell.number_format = _MONEY
        cell.font = Font(bold=True)

    note_row = total_row + 2
    worksheet.cell(row=note_row, column=1,
                   value=f"נוצר: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    skipped = report.get("skipped_tickers") or []
    if skipped:
        # Without this the totals quietly exclude them and the file reads as
        # a complete picture of the portfolio when it is not.
        cell = worksheet.cell(
            row=note_row + 1, column=1,
            value="לא נכללו (לא ניתן היה לשלוף מחיר): " + ", ".join(skipped),
        )
        cell.font = Font(bold=True, color="B00020")


def build_workbook(report: dict, journal: dict) -> bytes:
    """Render the holdings table, and the trade journal, as a .xlsx file.

    Takes the same dictionaries the API serves, so the sheet is by construction
    the table on the screen.
    """
    sheets = {
        "התיק שלי": _positions_frame(report),
        "יומן מסחר": _journal_frame(journal.get("entries") or []),
    }

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
            _style(writer.sheets[name], frame)
            if name == "התיק שלי":
                _append_totals(writer.sheets[name], frame, report)

    return buffer.getvalue()


def filename(today: Optional[date] = None) -> str:
    return f"portfolio-{(today or date.today()).isoformat()}.xlsx"
