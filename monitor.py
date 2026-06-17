import os
import json
import re
import logging
from datetime import datetime
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from firecrawl import FirecrawlApp
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

firecrawl = FirecrawlApp(api_key=os.environ["FIRECRAWL_API_KEY"])
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

SCAN_TARGETS = [
    {
        "site": "Walmart",
        "urls": [
            "https://www.walmart.com/browse/electronics/televisions/3944_1060825_447913",
            "https://www.walmart.com/browse/electronics/video-games/3944_7551309",
            "https://www.walmart.com/browse/electronics/computers/3944_3951_1089430",
            "https://www.walmart.com/browse/appliances/3736_90548",
        ],
    },
    {
        "site": "Target",
        "urls": [
            "https://www.target.com/c/tvs-home-theater-electronics/-/N-5xsx0",
            "https://www.target.com/c/video-games-electronics/-/N-5xsxd",
            "https://www.target.com/c/appliances/-/N-55atp",
        ],
    },
    {
        "site": "Best Buy",
        "urls": [
            "https://www.bestbuy.com/site/tvs/all-flat-screen-tvs/pcmcat159700050011.c",
            "https://www.bestbuy.com/site/video-games/pcmcat142200050005.c",
            "https://www.bestbuy.com/site/appliances/pcmcat319900050000.c",
        ],
    },
    {
        "site": "Home Depot",
        "urls": [
            "https://www.homedepot.com/b/Appliances/N-5yc1vZc3pi",
            "https://www.homedepot.com/b/Appliances-Refrigerators/N-5yc1vZc3poZ1z175gu",
        ],
    },
    {
        "site": "Amazon",
        "urls": [
            "https://www.amazon.com/s?i=electronics&bbn=172282&rh=n%3A172282%2Cn%3A1266092011",
            "https://www.amazon.com/s?i=videogames&bbn=468642&rh=n%3A468642",
            "https://www.amazon.com/s?i=appliances&bbn=2619526011",
        ],
    },
]

PRICE_JUDGE_PROMPT = """You are a price error detector. I will give you a list of products with their listed prices from a retail website.

For each product, determine if the price looks like an OBVIOUS pricing error — meaning the price is absurdly low compared to what this type of product normally retails for. Examples:
- A 65" OLED TV listed at $49
- A PlayStation 5 listed at $12
- A refrigerator listed at $8
- A laptop listed at $0.99

Do NOT flag:
- Normal sale prices (20-50% off)
- Refurbished items at lower prices
- Accessories or small items at low prices
- Products where low price makes sense

For each product, respond in this exact format (one line per product):
PRODUCT_INDEX|YES or NO|REASON

Only flag obvious errors where the price is clearly wrong by a massive amount.

Products to analyze:
{product_list}"""


def scrape_products(url: str, site: str) -> list[dict]:
    """Scrape product listings from a URL using Firecrawl."""
    try:
        result = firecrawl.scrape_url(url, formats=["markdown"])
        markdown = result.markdown if hasattr(result, "markdown") else result.get("markdown", "")
        if not markdown:
            log.warning(f"  [{site}] No content returned from {url}")
            return []

        # Use Claude to extract products from the raw markdown
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Extract all product listings from this retail page markdown. "
                        f"Return ONLY a JSON array of objects with keys: name, price (number, no $ sign), url. "
                        f"Only include items that have both a name and a numeric price. "
                        f"Return up to 100 items. Return ONLY the JSON array, no other text.\n\n"
                        f"{markdown[:12000]}"
                    ),
                }
            ],
        )
        text = response.content[0].text.strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        products = json.loads(match.group())
        for p in products:
            p["site"] = site
            p["source_url"] = url
        log.info(f"  [{site}] {url} → {len(products)} products")
        return products
    except Exception as e:
        log.warning(f"  [{site}] Failed to scrape {url}: {e}")
        return []


def check_prices_with_claude(products: list[dict]) -> list[dict]:
    """Send a batch of products to Claude for price error detection."""
    if not products:
        return []

    product_list = "\n".join(
        f"{i}. {p['name']} | ${p.get('price', 'N/A')}"
        for i, p in enumerate(products)
    )

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": PRICE_JUDGE_PROMPT.format(product_list=product_list),
                }
            ],
        )
        text = response.content[0].text
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return []

    flagged = []
    for line in text.strip().splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        idx_str, verdict, reason = parts
        if verdict.strip().upper() != "YES":
            continue
        try:
            idx = int(idx_str.strip())
            product = products[idx]
            flagged.append({**product, "reason": reason.strip()})
        except (ValueError, IndexError):
            continue

    return flagged


def send_sms_alert(product: dict):
    """Send an SMS alert for a flagged price error."""
    name = product.get("name", "Unknown Product")
    price = product.get("price", "N/A")
    reason = product.get("reason", "")
    site = product.get("site", "")
    url = product.get("url") or product.get("source_url", "No link available")

    body = (
        f"PRICE ERROR ALERT\n"
        f"Site: {site}\n"
        f"Product: {name}\n"
        f"Listed Price: ${price}\n"
        f"Reason: {reason}\n"
        f"Link: {url}"
    )

    try:
        twilio.messages.create(
            body=body,
            from_=os.environ["TWILIO_FROM_NUMBER"],
            to=os.environ["TWILIO_TO_NUMBER"],
        )
        log.info(f"SMS sent for: {name} @ ${price}")
    except Exception as e:
        log.error(f"Failed to send SMS for {name}: {e}")


def run_scan():
    """Run a full scan across all configured sites and categories."""
    log.info(f"=== Scan started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    total_products = 0
    total_flagged = 0

    for target in SCAN_TARGETS:
        site = target["site"]
        log.info(f"Scanning {site}...")
        all_products = []

        for url in target["urls"]:
            products = scrape_products(url, site)
            all_products.extend(products)

        total_products += len(all_products)
        log.info(f"  [{site}] Total products scraped: {len(all_products)}")

        # Process in batches of 50 to avoid overly long prompts
        batch_size = 50
        for i in range(0, len(all_products), batch_size):
            batch = all_products[i : i + batch_size]
            flagged = check_prices_with_claude(batch)
            total_flagged += len(flagged)

            for product in flagged:
                log.warning(
                    f"PRICE ERROR: {product['name']} @ ${product['price']} on {site} — {product['reason']}"
                )
                send_sms_alert(product)

    log.info(
        f"=== Scan complete: {total_products} products scanned, {total_flagged} errors flagged ==="
    )


def main():
    log.info("Price Error Monitor starting up...")
    log.info("Running initial scan...")
    run_scan()

    scheduler = BlockingScheduler()
    scheduler.add_job(run_scan, "interval", minutes=15, id="price_scan")
    log.info("Scheduler started — scanning every 15 minutes. Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Monitor stopped.")


if __name__ == "__main__":
    main()
