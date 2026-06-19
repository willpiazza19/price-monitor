import os
import time
import logging
from datetime import datetime
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
import feedparser
import anthropic
from twilio.rest import Client as TwilioClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor.log"),
    ],
)
log = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

USER_AGENT = "price-monitor-bot/1.0 (personal deal alerter)"

# Public deal feeds — RSS/Atom, designed to be read by bots, never blocked.
FEEDS = [
    {"name": "Slickdeals Frontpage", "url": "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1"},
    {"name": "Slickdeals Popular", "url": "https://feeds.feedburner.com/SlickdealsnetFP"},
    {"name": "r/deals", "url": "https://www.reddit.com/r/deals/new/.rss"},
    {"name": "r/buildapcsales", "url": "https://www.reddit.com/r/buildapcsales/new/.rss"},
    {"name": "r/GameDeals", "url": "https://www.reddit.com/r/GameDeals/new/.rss"},
    {"name": "r/Frugal_Tech", "url": "https://www.reddit.com/r/Frugal_Tech/new/.rss"},
]

# What you care about. Edit this freely — Claude uses it to decide what to alert on.
INTERESTS = """TVs (4K, OLED, QLED), electronics, gaming consoles (PlayStation, Xbox, Nintendo),
video games, laptops and computers, and home appliances (refrigerators, washers, dryers, dishwashers).
The goal is to catch genuinely strong deals — steep discounts, all-time-low prices, or items with
good resale value."""

MAX_ALERTS_PER_CYCLE = 5  # avoid getting blasted with texts

DEAL_JUDGE_PROMPT = """You are a deal-evaluation assistant. The user resells consumer electronics and wants alerts ONLY for genuinely strong deals matching their interests.

User's interests:
{interests}

Below is a numbered list of deals pulled from deal-aggregator feeds. Select ONLY the ones that are BOTH:
1. Clearly in the user's interest categories, AND
2. Genuinely strong deals (steep discount, notable price, or good resale potential).

Be SELECTIVE. It is better to flag 1-2 great deals than 10 mediocre ones. Skip generic, low-value, or accessory deals.

For each deal you select, output exactly one line in this format:
INDEX|REASON

Where INDEX is the number and REASON is a short phrase on why it's worth it. Output nothing for deals you don't select. If none qualify, output nothing.

Deals:
{deal_list}"""

# In-memory record of deals we've already processed. Reset on restart (which is fine —
# we re-seed silently so you never get spammed with old deals after a redeploy).
seen_ids = set()
first_run = True


def fetch_feed(feed: dict) -> list[dict]:
    """Fetch and parse one RSS/Atom feed."""
    try:
        parsed = feedparser.parse(feed["url"], agent=USER_AGENT)
        entries = []
        for e in parsed.entries:
            link = e.get("link", "")
            key = e.get("id") or link or e.get("title", "")
            if not key:
                continue
            entries.append(
                {
                    "key": key,
                    "title": e.get("title", "").strip(),
                    "link": link,
                    "source": feed["name"],
                }
            )
        log.info(f"  [{feed['name']}] {len(entries)} items")
        return entries
    except Exception as e:
        log.warning(f"  [{feed['name']}] Failed to fetch: {e}")
        return []


def judge_deals(deals: list[dict]) -> list[dict]:
    """Ask Claude which deals are worth alerting on."""
    if not deals:
        return []

    deal_list = "\n".join(f"{i}. {d['title']} ({d['source']})" for i, d in enumerate(deals))

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": DEAL_JUDGE_PROMPT.format(interests=INTERESTS, deal_list=deal_list),
                }
            ],
        )
        text = response.content[0].text
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return []

    flagged = []
    for line in text.strip().splitlines():
        parts = line.strip().split("|", 1)
        if len(parts) != 2:
            continue
        idx_str, reason = parts
        try:
            idx = int(idx_str.strip())
            flagged.append({**deals[idx], "reason": reason.strip()})
        except (ValueError, IndexError):
            continue

    return flagged


def send_sms_alert(deal: dict):
    """Send an SMS alert for a flagged deal."""
    body = (
        f"DEAL ALERT ({deal['source']})\n"
        f"{deal['title']}\n"
        f"Why: {deal['reason']}\n"
        f"Link: {deal['link']}"
    )
    try:
        twilio.messages.create(
            body=body,
            from_=os.environ["TWILIO_FROM_NUMBER"],
            to=os.environ["TWILIO_TO_NUMBER"],
        )
        log.info(f"SMS sent: {deal['title']}")
    except Exception as e:
        log.error(f"Failed to send SMS for '{deal['title']}': {e}")


def run_scan():
    """Fetch all feeds, find new deals, judge them, and alert."""
    global first_run
    log.info(f"=== Scan started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")

    new_deals = []
    for feed in FEEDS:
        for entry in fetch_feed(feed):
            if entry["key"] not in seen_ids:
                seen_ids.add(entry["key"])
                new_deals.append(entry)
        time.sleep(2)  # avoid Reddit rate-limiting on rapid sequential requests

    if first_run:
        log.info(f"Seeded {len(seen_ids)} existing deals (no alerts on first run).")
        first_run = False
        log.info("=== Scan complete (initial seed) ===")
        return

    log.info(f"{len(new_deals)} new deals since last scan")

    flagged = judge_deals(new_deals)
    for deal in flagged[:MAX_ALERTS_PER_CYCLE]:
        log.warning(f"DEAL: {deal['title']} — {deal['reason']}")
        send_sms_alert(deal)

    log.info(
        f"=== Scan complete: {len(new_deals)} new, {len(flagged)} matched, "
        f"{min(len(flagged), MAX_ALERTS_PER_CYCLE)} alerts sent ==="
    )


def send_startup_text():
    """Send a one-time confirmation text on boot so you know the monitor is live."""
    try:
        twilio.messages.create(
            body="Deal Monitor is now LIVE and watching for deals. (This is a startup test.)",
            from_=os.environ["TWILIO_FROM_NUMBER"],
            to=os.environ["TWILIO_TO_NUMBER"],
        )
        log.info("Startup confirmation text sent.")
    except Exception as e:
        log.error(f"Startup text failed (check Twilio credentials): {e}")


def main():
    log.info("Deal Alert Monitor starting up...")
    send_startup_text()
    log.info("Running initial scan (seeding existing deals)...")
    run_scan()

    scheduler = BlockingScheduler()
    scheduler.add_job(run_scan, "interval", minutes=10, id="deal_scan")
    log.info("Scheduler started — scanning every 10 minutes. Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Monitor stopped.")


if __name__ == "__main__":
    main()
