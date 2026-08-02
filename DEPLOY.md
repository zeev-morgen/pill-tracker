# פריסה: בסיס נתונים + גישה מכל מכשיר

מדריך מלא להעלאת המערכת לאוויר בחינם, עם כתובת קבועה שעובדת מכל מכשיר
ונתונים שנשמרים לצמיתות. זמן משוער: 20–30 דקות.

הארכיטקטורה: **Neon** (בסיס הנתונים) + **Render** (הרצת המערכת) +
**UptimeRobot** (שמירה על המערכת ערה). כל השלושה חינמיים.

---

## שלב 1 — בסיס נתונים ב-Neon

בחרתי ב-Neon ולא ב-PostgreSQL החינמי של Render מסיבה אחת: **ה-DB של Render
נמחק אוטומטית אחרי 30 יום**. של Neon לא פג תוקף.

1. היכנס ל-https://neon.tech והירשם (אפשר עם חשבון GitHub).
2. **Create project** — תן שם, למשל `stock-monitor`. בחר Region קרוב
   (`AWS eu-central-1` לישראל).
3. אחרי היצירה מוצג **Connection string**. העתק אותו — נראה כך:

   ```
   postgresql://neondb_owner:AbC123xyz@ep-cool-name-123456.eu-central-1.aws.neon.tech/neondb?sslmode=require
   ```

   שמור אותו בצד, הוא נדרש בשלב הבא. אם איבדת אותו: Dashboard → Connection Details.

> אין צורך ליצור טבלאות. המערכת יוצרת את `alerts` ו-`holdings` לבד בהפעלה
> הראשונה.

---

## שלב 2 — הרצת המערכת ב-Render

1. היכנס ל-https://render.com והירשם עם GitHub.
2. **New → Blueprint** → בחר את הריפו `pill-tracker` ואת הענף
   `claude/stock-tracker-upgrade-6ejl2b`.
   Render יזהה את `render.yaml` ויציע שירות בשם `stock-monitor`.
3. לפני האישור, מלא את משתני הסביבה:

   | משתנה | ערך |
   |---|---|
   | `DATABASE_URL` | ה-connection string מ-Neon (שלב 1) |
   | `DASHBOARD_USER` | שם משתמש שתמציא, למשל `zeev` |
   | `DASHBOARD_PASSWORD` | **סיסמה חזקה** — הדשבורד ייחשף לאינטרנט |
   | `ANTHROPIC_API_KEY` | המפתח מ-console.anthropic.com |
   | `TELEGRAM_BOT_TOKEN` | הטוקן מ-BotFather |
   | `TELEGRAM_CHAT_ID` | ה-chat_id שלך (`6213444015`) |
   | `TRADINGVIEW_WEBHOOK_SECRET` | מחרוזת אקראית כלשהי |

4. **Apply** ← הבנייה נמשכת כ-3–5 דקות. בסיום תקבל כתובת בסגנון
   `https://stock-monitor-XXXX.onrender.com`.

5. פתח את הכתובת — הדפדפן יבקש שם משתמש וסיסמה. זו הכתובת שתעבוד
   **מכל מכשיר**: טלפון, טאבלט, מחשב בעבודה.

> **חשוב — כבה את הבוט המקומי.** שני עותקים של בוט הטלגרם באותו טוקן
> "גונבים" הודעות זה מזה. אחרי שהענן עלה, עצור את `python run.py` במחשב
> (Ctrl+C), או הרץ אותו בלי `TELEGRAM_BOT_TOKEN`.

---

## שלב 3 — שמירה על המערכת ערה (קריטי)

המסלול החינמי של Render **מכבה את השירות אחרי 15 דקות ללא תנועה**. עבור
מערכת ניטור זו בעיה אמיתית: כשהיא כבויה אין סריקות, אין התראות והבוט לא
עונה. הפתרון: שירות שמבצע פינג קבוע.

1. הירשם ל-https://uptimerobot.com (חינם, עד 50 מוניטורים).
2. **Add New Monitor**:
   - Monitor Type: **HTTP(s)**
   - Friendly Name: `stock-monitor`
   - URL: `https://<הכתובת-שלך>.onrender.com/health`
   - Monitoring Interval: **5 minutes**
3. שמור.

נתיב `/health` נבחר בכוונה — הוא הנתיב היחיד שנשאר פתוח בלי סיסמה, כדי
שהפינג יעבוד בלי לחשוף שום מידע. הוא מחזיר רק חותמת זמן.

בונוס: UptimeRobot ישלח לך התראה במייל אם המערכת נופלת.

---

## אימות שהכל עובד

לאחר הפריסה, בדוק בלוגים של Render (Dashboard → Logs) שמופיע:

```
stock_monitor.db: PostgreSQL connected — alerts and holdings are persisted
stock_monitor.main: Telegram command bot active
```

אם במקום השורה הראשונה מופיע `DATABASE_URL not set` — המשתנה לא הוגדר
נכון, והנתונים לא יישמרו.

בדיקה מעשית:
1. פתח את הדשבורד בטלפון והוסף פוזיציה בטאב "התיק שלי".
2. ב-Render: **Manual Deploy → Restart service**.
3. רענן את הדשבורד — הפוזיציה עדיין שם. זו ההוכחה שה-DB עובד.

---

## מגבלות המסלול החינמי

- **750 שעות חודשיות** ב-Render — מספיקות לשירות אחד שרץ ברציפות.
- **הפעלה ראשונה איטית** אחרי חוסר פעילות ארוך (עד 50 שניות). עם
  UptimeRobot זה כמעט לא קורה.
- **Neon**: 0.5GB אחסון — יותר מספיק להיסטוריית התראות ולתיק.
- הבנייה מחדש בכל `git push` לענף (deploy אוטומטי).

---

## הרצה מקומית במקביל

אפשר להמשיך לפתח מקומית מול אותו DB — הוסף ל-`.env`:

```
DATABASE_URL=postgresql://...  # אותו connection string מ-Neon
```

כך התיק וההתראות משותפים בין הענן למחשב. רק זכור: **בלי
`TELEGRAM_BOT_TOKEN` מקומית**, אחרת שני הבוטים יתנגשו.
