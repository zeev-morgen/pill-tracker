# Stock Monitor — Real-Time Alert Daemon

A production-grade Python daemon that watches NYSE/NASDAQ stocks 24/5, evaluates
custom alert rules, and pushes instant notifications via **Telegram**, **Discord**,
and/or **desktop popups**.  TradingView can send webhook alerts directly into the
app through the built-in FastAPI server.

```
┌─────────────────────────────────────────────────────────────────┐
│  yfinance (pre/regular/after-hours data)                        │
│       │                                                          │
│  Alert Engine  ──► price_change_pct / volume_spike / threshold  │
│       │                                                          │
│  Notification Dispatcher ──► Telegram / Discord / Desktop       │
│                                                                  │
│  FastAPI  ◄── TradingView Webhook  ──► Dispatcher               │
└─────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### 1. Clone & create a virtual environment

```bash
git clone <repo-url>
cd pill-tracker
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure API keys

```bash
cp .env.example .env
$EDITOR .env          # fill in tokens (see the sections below)
```

### 3. Configure stocks and alert rules

```bash
$EDITOR config/config.yaml
```

### 4. Run

```bash
python run.py
```

---

## Project Layout

```
stock_monitor/
├── config.py          # YAML config loader with ${ENV_VAR} interpolation
├── data_feed.py        # yfinance wrapper — real-time + pre/after-hours
├── alert_engine.py    # Evaluates price_change_pct, volume_spike, price_threshold
├── notifier.py        # Telegram / Discord / Desktop notification dispatch
├── webhook_server.py  # FastAPI endpoints for TradingView & custom webhooks
├── scheduler.py       # APScheduler with NYSE timezone + market-hours gate
└── main.py            # Application wiring & graceful shutdown
config/
└── config.yaml        # All settings (edit this)
run.py                 # CLI entry point
stock-monitor.service  # systemd unit (for daemon mode)
```

---

## Alert Rule Reference

Each stock in `config.yaml` can have an `alerts` list.  Mix and match rule types.

### `price_change_pct` — percentage move in a time window

```yaml
- type: price_change_pct
  threshold_pct: 2.0        # alert if |Δ%| ≥ this value
  window_minutes: 10        # look-back window (rolling, in memory)
  cooldown_minutes: 30      # min gap between repeat alerts
```

> **Example:** "Alert me if $INTC moves ±2% in any 10-minute window."

### `volume_spike` — unusual volume vs. 10-day average

```yaml
- type: volume_spike
  multiplier: 2.0           # alert if volume ≥ multiplier × the time-adjusted benchmark
  time_adjusted: true       # default true — measure against elapsed trading time
  cooldown_minutes: 60
```

By default (`time_adjusted: true`) the benchmark is scaled to **how much of the
trading session has elapsed**, not the full-day average. So if the market has
been open 3 hours (~46% of the 9:30–16:00 session) and the stock has already
traded a full day's average volume, that counts as ~2.2× the expected pace and
fires immediately — instead of waiting until the raw daily total is exceeded.
Pre-market and after-hours fall back to the plain full-day comparison.

Set `time_adjusted: false` to keep the old behaviour (compare cumulative volume
against the full 10-day daily average regardless of time of day).

> **Example:** "Alert if the stock is trading at 2× its normal pace for this
> point in the day — e.g. a full day's volume done in the first 3 hours."

### `price_threshold` — cross a fixed price level

```yaml
- type: price_threshold
  above: 230.00             # alert when price ≥ this level
  below: 18.00              # alert when price ≤ this level (use one or both)
  cooldown_minutes: 120
```

> **Example:** "Alert when $AAPL breaks above $230."

---

## Notification Channels

### Telegram Bot

1. Open Telegram → search **@BotFather** → `/newbot` → follow prompts.
2. Copy the **bot token** into `.env` as `TELEGRAM_BOT_TOKEN`.
3. Start a conversation with your bot, then visit:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Find `"chat":{"id": <NUMBER>}` — that is your `TELEGRAM_CHAT_ID`.
4. In `config.yaml`:
   ```yaml
   notifications:
     telegram:
       enabled: true
   ```

### Discord Webhook

1. Open Discord → Server Settings → **Integrations** → **Webhooks** → **New Webhook**.
2. Choose a channel → **Copy Webhook URL** → paste into `.env` as `DISCORD_WEBHOOK_URL`.
3. In `config.yaml`:
   ```yaml
   notifications:
     discord:
       enabled: true
   ```

### Desktop Notifications (Linux / macOS / Windows)

Enabled by default — requires `plyer` (already in `requirements.txt`).
On Linux you may also need `libnotify`:
```bash
sudo apt install libnotify-bin
```

---

## TradingView Webhook Integration

### 1. Expose the webhook server to the internet

The app listens on `http://0.0.0.0:8080`.  TradingView needs a **public HTTPS URL**.

**Option A — ngrok (quick testing)**
```bash
ngrok http 8080
# Output: Forwarding  https://abc123.ngrok.io → localhost:8080
```

**Option B — nginx reverse proxy with Let's Encrypt (production)**
```nginx
server {
    listen 443 ssl;
    server_name alerts.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/alerts.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/alerts.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
    }
}
```

### 2. Configure TradingView

1. Open a chart → right-click indicator/strategy → **Add Alert**.
2. In the **Notifications** tab → enable **Webhook URL**.
3. Enter your URL:
   ```
   https://YOUR_DOMAIN_OR_NGROK/webhook/tradingview
   ```
4. Set the **Message** body to JSON (TradingView supports dynamic variables):
   ```json
   {
     "ticker":  "{{ticker}}",
     "close":   {{close}},
     "volume":  {{volume}},
     "message": "Alert triggered on {{ticker}} at {{close}}"
   }
   ```
5. Click **Create**.

### 3. Optional: HMAC signature verification

Set `TRADINGVIEW_WEBHOOK_SECRET` in `.env` to a random string.
Then add a custom header in TradingView's webhook configuration:
```
X-Signature: sha256=<HMAC_SHA256(secret, body)>
```
(TradingView Pro/Premium supports custom headers via Pine Script's `alert()` function.)

### Testing the webhook manually

```bash
curl -X POST http://localhost:8080/webhook/tradingview \
     -H "Content-Type: application/json" \
     -d '{"ticker":"AAPL","close":195.50,"message":"Test alert from TradingView"}'
```

---

## Running as a Background Service (systemd)

```bash
# 1. Edit the service file with your username and path
nano stock-monitor.service

# 2. Install it
sudo cp stock-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload

# 3. Enable auto-start on boot and start immediately
sudo systemctl enable --now stock-monitor

# 4. Check status
sudo systemctl status stock-monitor

# 5. Follow live logs
sudo journalctl -u stock-monitor -f
```

To stop/restart:
```bash
sudo systemctl stop stock-monitor
sudo systemctl restart stock-monitor
```

---

## Timezone Handling

All scheduling and market-hour detection uses **America/New_York (ET)** via `pytz`.

| Session    | ET Window      |
|------------|----------------|
| Pre-market | 04:00 – 09:30  |
| Regular    | 09:30 – 16:00  |
| After-hours| 16:00 – 20:00  |
| Closed     | outside above  |

Set `monitoring.include_extended_hours: false` in `config.yaml` to restrict
polling to regular-hours only.

---

## Environment Variables Reference

| Variable                    | Required | Description                          |
|-----------------------------|----------|--------------------------------------|
| `TELEGRAM_BOT_TOKEN`        | Telegram | Bot token from @BotFather            |
| `TELEGRAM_CHAT_ID`          | Telegram | Your personal or group chat ID       |
| `DISCORD_WEBHOOK_URL`       | Discord  | Full Discord webhook URL             |
| `TRADINGVIEW_WEBHOOK_SECRET`| Optional | HMAC secret for webhook verification |

---

## Logs

Application logs rotate at **10 MB** and keep **5 backups**:
```
logs/stock_monitor.log
logs/stock_monitor.log.1
…
```

Change the log level in `config.yaml`:
```yaml
logging:
  level: "DEBUG"   # DEBUG | INFO | WARNING | ERROR
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `No data for AAPL` | yfinance is rate-limited — wait 60 s or reduce polling frequency |
| Telegram sends fail | Check `TELEGRAM_BOT_TOKEN` and that you started a chat with the bot |
| Desktop notifications missing | Install `libnotify-bin` on Linux |
| TradingView webhooks not received | Ensure port 8080 is reachable from the internet; check firewall rules |
| Service fails to start | Run `python run.py` manually and read stderr |

---

## API Endpoints

| Method | Path                      | Description                       |
|--------|---------------------------|-----------------------------------|
| GET    | `/health`                 | Liveness probe                    |
| GET    | `/docs`                   | Interactive API docs (Swagger UI) |
| POST   | `/webhook/tradingview`    | TradingView alert receiver        |
| POST   | `/webhook/custom`         | Generic JSON alert endpoint       |
