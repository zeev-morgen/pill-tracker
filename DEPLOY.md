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

## שלב 4 — התראות למייל (אופציונלי)

נדרש **רק** אם תבקש התראה במייל ("תתריע לי *במייל* כש..."). כל השאר —
טלגרם, הדשבורד, הסיכום היומי — עובד בלי זה.

### 4א. יצירת App Password בגוגל

ג'ימייל דוחה סיסמת חשבון רגילה ב-SMTP (שגיאה 535), ולכן צריך סיסמה
ייעודית לאפליקציה:

1. **הפעל אימות דו-שלבי**, אם עוד לא מופעל:
   Google Account → Security → "How you sign in to Google" → 2-Step Verification.
   בלי זה האפשרות ליצור App Password כלל לא מופיעה.
2. היכנס ישירות ל-https://myaccount.google.com/apppasswords
   (גוגל לא תמיד מציגה את הקישור בתוך תפריט Security).
3. תן שם כלשהו, למשל `stock-monitor`, ולחץ **Create**.
4. גוגל תציג **16 תווים פעם אחת בלבד**. העתק אותם מיד. אפשר להעתיק
   בלי הרווחים.

> אם החשבון רשום ל-Advanced Protection, או שמדובר בחשבון Workspace
> שהארגון חסם בו App Passwords — האפשרות לא תהיה זמינה.

### 4ב. הגדרה ב-Render

Dashboard → השירות `stock-monitor` → **Environment** → Add Environment
Variable, חמש פעמים:

| משתנה | ערך |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | כתובת הג'ימייל שלך |
| `SMTP_PASSWORD` | 16 התווים מסעיף 4א — **לא** סיסמת החשבון |
| `ALERT_EMAIL_TO` | לאן לשלוח. אם תשאיר ריק — יישלח ל-`SMTP_USER` |

**Save, Rebuild and Deploy** ← Render מפעיל את השירות מחדש (כדקה).

פורט 587 הוא STARTTLS וזו ברירת המחדל. 465 גם עובד (TLS מלא). **פורט 25
חסום ב-Render** ואין טעם לנסות אותו.

### 4ג. אימות

פתח `https://<הכתובת-שלך>.onrender.com/api/diagnostics/email`.
המצב התקין:

```json
{"configured": true, "host": "smtp.gmail.com", "port": 587,
 "user": "you@gmail.com", "to": "you@gmail.com", "missing": []}
```

הנתיב הזה **לא מחזיר את הסיסמה** אף פעם — רק אם קיימת. אם משתנה חסר,
שמו יופיע ב-`missing`.

בדיקה מעשית: שלח לבוט
`תתריע לי במייל כשמניית AVGO עולה 0.5% מנקודת הפתיחה` ואשר. בפעם הבאה
שהתנאי יתקיים, המייל יישלח ואישור קצר יופיע בטלגרם.

> **אם המייל לא מוגדר או שהשליחה נכשלה — ההתראה לא נאבדת.** היא תגיע
> לטלגרם עם שורה שאומרת מה חסר. זה המקרה שחשוב שלא ייעלם בשקט.

מגבלת ג'ימייל פרטי היא כ-500 מיילים ביום — בלתי רלוונטי לשימוש הזה.

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
