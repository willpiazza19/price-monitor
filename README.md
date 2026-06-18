# Deal Alert Monitor

Watches public deal feeds (Slickdeals + Reddit deal communities) and texts you when a genuinely strong deal matching your interests appears. No web scraping, no anti-bot fights, no Firecrawl — just reliable public RSS feeds + Claude to filter for the good stuff.

## How it works

1. **Reads** public deal feeds every 10 minutes (Slickdeals, r/deals, r/buildapcsales, r/GameDeals, etc.)
2. **Filters** new deals with Claude (`claude-sonnet-4-6`) against your interests — only the genuinely strong ones pass
3. **Texts** you via Twilio with the deal title, why it's good, and a direct link

On startup it silently records all current deals (so you aren't blasted with old ones), then only alerts on *new* deals going forward.

---

## Setup

### 1. Install dependencies
```bash
cd price-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure credentials
```bash
cp .env.example .env
open .env
```

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `TWILIO_ACCOUNT_SID` | [console.twilio.com](https://console.twilio.com) → Account Info |
| `TWILIO_AUTH_TOKEN` | [console.twilio.com](https://console.twilio.com) → Account Info |
| `TWILIO_FROM_NUMBER` | Your Twilio phone number (e.g. `+15005550006`) |
| `TWILIO_TO_NUMBER` | Your personal number to receive alerts |

> Firecrawl is **no longer needed** — you can stop paying for it.

### 3. Run
```bash
python monitor.py
```

---

## Customizing

Open `monitor.py` and edit:

- **`INTERESTS`** — describe in plain English what deals you want. Claude uses this to decide what to alert on.
- **`FEEDS`** — add or remove deal feeds. Any RSS/Atom URL works (most subreddits support `https://www.reddit.com/r/NAME/new/.rss`).
- **`MAX_ALERTS_PER_CYCLE`** — cap how many texts you get per scan (default 5).
- The scan interval (default 10 minutes) is set in `main()`.

---

## Logs

All activity is logged to the console and to `monitor.log`, showing each scan, how many new deals were found, how many matched, and how many alerts were sent.
