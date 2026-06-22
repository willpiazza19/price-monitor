import os
import time
import logging
from datetime import datetime
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
import feedparser
import requests
import anthropic

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

# --- Alert channels (configure either Telegram or Twilio) ---
# Telegram is preferred if set; otherwise we fall back to Twilio SMS.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
TWILIO_TO_NUMBER = os.environ.get("TWILIO_TO_NUMBER", "").strip()

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    from twilio.rest import Client as TwilioClient
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

USER_AGENT = "price-monitor-bot/1.0 (personal deal alerter)"

# Deal feeds tuned to what Eric actually wants. Slickdeals keyword searches + the
# menswear-deals community. RSS/Atom — designed to be read by bots, never blocked.
def _sd(query):
    return f"https://slickdeals.net/newsearch.php?q={query}&searcharea=deals&searchin=first&rss=1"

FEEDS = [
    # Golf & fishing
    {"name": "Slickdeals golf", "url": _sd("golf")},
    {"name": "Slickdeals fishing", "url": _sd("fishing")},
    # Clothing brands
    {"name": "Slickdeals lululemon", "url": _sd("lululemon")},
    {"name": "Slickdeals vuori", "url": _sd("vuori")},
    {"name": "Slickdeals chubbies", "url": _sd("chubbies")},
    {"name": "Slickdeals Peter Millar", "url": _sd("peter+millar")},
    {"name": "Slickdeals Johnnie-O", "url": _sd("johnnie-o")},
    {"name": "Slickdeals Vineyard Vines", "url": _sd("vineyard+vines")},
    {"name": "Slickdeals Rhoback", "url": _sd("rhoback")},
    # Menswear deals community (Lululemon, Vuori, etc. show up here constantly)
    {"name": "r/frugalmalefashion", "url": "https://www.reddit.com/r/frugalmalefashion/new/.rss"},
    # Fragrances (designer + niche)
    {"name": "Slickdeals cologne", "url": _sd("cologne")},
    {"name": "Slickdeals fragrance", "url": _sd("fragrance")},
    {"name": "r/fragrancedeals", "url": "https://www.reddit.com/r/fragrancedeals/new/.rss"},
]

# What you care about. Edit this freely — Claude uses it to decide what to alert on.
INTERESTS = """I am shopping for MYSELF (not reselling). Alert me ONLY on standout deals —
steep discounts, clearance, or all-time-low prices — on these:

GOLF: golf clubs (drivers, irons, putters, wedges), golf balls, golf gloves, golf bags.
FISHING: fishing rods/poles, reels, hooks, weights/sinkers, tackle.
CLOTHING (these brands specifically): Lululemon, Vuori, Chubbies, Peter Millar,
Johnnie-O, Vineyard Vines, Rhoback.

FRAGRANCES: designer and niche colognes/fragrances — but ONLY from reputable, established
retailers (e.g. FragranceX, FragranceNet, Jomashop, Notino, Macy's, Nordstrom, Sephora, Ulta,
or the brand's official site). Skip unknown/sketchy fragrance sites and obvious fakes.

Ignore anything outside these. Ignore mediocre or everyday discounts — only genuinely
strong deals worth jumping on for personal use."""

MAX_ALERTS_PER_CYCLE = 20  # ~5 per category (golf / fishing / clothing / fragrance)

DEAL_JUDGE_PROMPT = """You are a personal-shopping deal filter. The user is buying for HIMSELF (not reselling), and only wants alerts for STANDOUT deals matching his interests.

User's interests:
{interests}

Below is a numbered list of deals from deal feeds. Select ONLY deals that are BOTH:
1. Clearly one of the user's wanted items or brands, AND
2. A STANDOUT deal — steep discount, clearance, or notably low / all-time-low price (NOT an everyday or mediocre discount).

Be very selective. Skip anything generic, marginal, or outside his interests. Aim for at most ~5 per category (golf, fishing, clothing, fragrance). For fragrances, only flag deals from reputable/established retailers — skip unknown or sketchy sites.

For each deal you select, output exactly one line in this format:
INDEX|REASON

Where REASON is a short phrase on why it's a standout. Output nothing for deals you don't select. If none qualify, output nothing.

Deals:
{deal_list}"""

# In-memory record of deals we've already processed. Reset on restart (which is fine —
# we re-seed silently so you never get spammed with old deals after a redeploy).
seen_ids = set()
first_run = True


def send_alert(text: str) -> bool:
    """Send an alert via Telegram (preferred) or Twilio SMS (fallback)."""
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False},
                timeout=15,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            log.error(f"Telegram send failed: {e}")
            return False

    if twilio_client and TWILIO_FROM_NUMBER and TWILIO_TO_NUMBER:
        try:
            twilio_client.messages.create(body=text, from_=TWILIO_FROM_NUMBER, to=TWILIO_TO_NUMBER)
            return True
        except Exception as e:
            log.error(f"Twilio send failed: {e}")
            return False

    log.warning("No alert channel configured (set Telegram or Twilio env vars).")
    return False


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


def alert_deal(deal: dict):
    """Format and send an alert for a flagged deal."""
    body = (
        f"DEAL ALERT ({deal['source']})\n"
        f"{deal['title']}\n"
        f"Why: {deal['reason']}\n"
        f"Link: {deal['link']}"
    )
    if send_alert(body):
        log.info(f"Alert sent: {deal['title']}")


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
        alert_deal(deal)

    log.info(
        f"=== Scan complete: {len(new_deals)} new, {len(flagged)} matched, "
        f"{min(len(flagged), MAX_ALERTS_PER_CYCLE)} alerts sent ==="
    )


def send_startup_alert():
    """Send a one-time confirmation on boot so you know the monitor is live."""
    if send_alert("Deal Monitor is now LIVE and watching for deals. (Startup test.)"):
        log.info("Startup confirmation alert sent.")
    else:
        log.error("Startup alert failed — check your Telegram or Twilio settings.")


def main():
    log.info("Deal Alert Monitor starting up...")
    channel = "Telegram" if (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) else "Twilio SMS" if twilio_client else "NONE"
    log.info(f"Alert channel: {channel}")
    send_startup_alert()
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
