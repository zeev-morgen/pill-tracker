# Stock Tracker

מערכת מעקב מניות: FastAPI + yfinance + ניתוח AI מבוסס Claude.

## הרצה מקומית

```bash
pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "sk-ant-..."      # PowerShell (ב-bash: export ...)
python -m uvicorn main:app --reload
```

הדשבורד: http://localhost:8000

## הזנה ועריכה של פוזיציות

לכל פוזיציה מזינים **טיקר, כמות ומחיר כניסה למניה** (הזנה ידנית — לא נשלף מהיסטוריה).
כפתור **עריכה** בכל שורה פותח את אותו טופס עם הערכים הקיימים; שמירה מעדכנת
את הפוזיציה הקיימת (הטיקר נעול בעריכה). הרווח/הפסד וניתוח ה-AI מחושבים
מול מחיר הכניסה שהזנת.

הנתונים נשמרים ב-`data/portfolio.json` (ניתן לשנות נתיב עם `STORE_PATH`).

## הגנת סיסמה

אופציונלי מקומית, **חובה בפריסה ציבורית**. הגדרת שני משתני סביבה מפעילה
HTTP Basic Auth על כל הדפים; כשהם לא מוגדרים אין דרישת התחברות:

```bash
DASHBOARD_USER=myname
DASHBOARD_PASSWORD=some-long-password
```

## פריסה חינמית (כתובת יציבה מכל מכשיר)

**Render — Free plan:** הריפו כולל `render.yaml`. בקונסולת Render בוחרים
New → Blueprint, מצביעים על הריפו והענף, ומגדירים בדשבורד את
`ANTHROPIC_API_KEY`, `DASHBOARD_USER` ו-`DASHBOARD_PASSWORD`. מקבלים כתובת
קבועה כמו `https://stock-tracker.onrender.com`.

שתי מגבלות של המסלול החינמי: השרת נכבה אחרי ~15 דקות חוסר פעילות
(הבקשה הראשונה אחריו איטית בכ-30 שניות), והדיסק אינו קבוע — כלומר
`data/portfolio.json` יימחק ב-deploy מחדש. לשמירה קבועה יש להוסיף
Persistent Disk (בתשלום) או להחליף ל-DB חיצוני.

**חלופה ללא הגבלות אלה:** Tailscale — התקנה חינמית על המחשב ועל הטלפון
נותנת גישה מכל רשת לשרת המקומי, עם הנתונים נשארים על המחשב שלך.

## מבנה

| קובץ | תפקיד |
|---|---|
| `store.py` | State של פוזיציות (טיקר, כמות, מחיר כניסה), thread-safe עם התמדה ל-JSON |
| `market_data.py` | שכבת yfinance: מחירים, היסטוריה, `_fetch_fundamentals` (סקטורים ומדדים) |
| `alert_engine.py` | חישוב ATR, התראות חשיפת תנודתיות והתראות פיזור סקטוריאלי |
| `ai_analyst.py` | `_build_prompt` עם הזרקת הפוזיציה האישית (מחיר כניסה ו-P/L) + קריאה ל-Claude |
| `dashboard.py` | UI: טופס הזנה/עריכה, טאבי התראות, תרשימי עוגה (Chart.js) |
| `main.py` | FastAPI — חיווט הרכיבים, endpoints והגנת הסיסמה |

## בדיקות

```bash
python -m pytest tests/ -v
```
