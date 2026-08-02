# Stock Tracker

מערכת מעקב מניות: FastAPI + yfinance + ניתוח AI מבוסס Claude.

## התקנה והרצה

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # נדרש לניתוחי ה-AI
export ANTHROPIC_MODEL=claude-opus-4-7     # אופציונלי (ברירת מחדל)
uvicorn main:app --reload
```

הדשבורד זמין ב-http://localhost:8000

## מבנה

| קובץ | תפקיד |
|---|---|
| `store.py` | ניהול State של פוזיציות ידניות (כמות + מועד רכישה), thread-safe עם התמדה ל-JSON |
| `market_data.py` | שכבת גישה ל-yfinance: מחירים, היסטוריה, `_fetch_fundamentals` (סקטורים ומדדים) |
| `alert_engine.py` | חישוב ATR, התראות חשיפת תנודתיות והתראות פיזור סקטוריאלי |
| `ai_analyst.py` | `_build_prompt` עם הזרקת נתוני הפוזיציה האישית + קריאה ל-Claude |
| `dashboard.py` | UI: טופס הזנה ידנית, טאבי התראות, תרשימי עוגה (Chart.js) |
| `main.py` | FastAPI — חיווט כל הרכיבים ו-API endpoints |

## בדיקות

```bash
pytest tests/ -v
```
