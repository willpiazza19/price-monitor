# Price Error Monitor

Automatically scans Walmart, Target, Best Buy, Home Depot, and Amazon every 15 minutes for obvious pricing errors and sends SMS alerts via Twilio.

## How it works

1. **Scrapes** product listings from electronics, TVs, gaming, and appliances categories using Firecrawl
2. **Analyzes** each product's price with Claude (`claude-sonnet-4-6`) to detect obvious errors (e.g., a 65" TV for $12)
3. **Sends SMS** via Twilio when a price error is detected, including the product name, price, reason, and direct link

---

## Setup

### 1. Clone / download the files

Make sure you have these files in a folder:
```
price-monitor/
├── monitor.py
├── requirements.txt
├── .env.example
└── README.md
```

### 2. Create a Python virtual environment (recommended)

```bash
cd price-monitor
python3 -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure credentials

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

Open `.env` and fill in:

| Variable | Where to get it |
|---|---|
| `FIRECRAWL_API_KEY` | [firecrawl.dev](https://firecrawl.dev) — sign up for a free or paid plan |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `TWILIO_ACCOUNT_SID` | [console.twilio.com](https://console.twilio.com) → Account Info |
| `TWILIO_AUTH_TOKEN` | [console.twilio.com](https://console.twilio.com) → Account Info |
| `TWILIO_FROM_NUMBER` | Your Twilio phone number (e.g. `+15005550006`) |
| `TWILIO_TO_NUMBER` | Your personal number to receive alerts (e.g. `+14155551234`) |

### 5. Run the monitor

```bash
python monitor.py
```

The monitor will:
- Run an immediate scan on startup
- Schedule a scan every 15 minutes automatically
- Log all activity to the console and to `monitor.log`

Press `Ctrl+C` to stop.

---

## What gets scanned

| Site | Categories |
|---|---|
| Walmart | TVs, Video Games, Computers, Appliances |
| Target | TVs/Home Theater, Video Games, Appliances |
| Best Buy | Flat Screen TVs, Video Games, Appliances |
| Home Depot | Appliances, Refrigerators |
| Amazon | Electronics, Video Games, Appliances |

---

## SMS alert format

When a price error is detected you'll receive a text like:

```
PRICE ERROR ALERT
Site: Best Buy
Product: Samsung 75" QLED 4K TV
Listed Price: $19
Reason: A 75-inch QLED TV typically retails for $800-$2500; $19 is clearly an error
Link: https://www.bestbuy.com/site/...
```

---

## Logs

All scans are logged to `monitor.log` in the same directory. Each entry shows the timestamp, number of products scanned, and any flagged errors.
