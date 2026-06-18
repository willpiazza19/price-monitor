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

SEARCH_QUERIES = [
    {"site": "Walmart", "query": "4K TV OLED QLED site:walmart.com"},
    {"site": "Walmart", "query": "PlayStation 5 Xbox Series X site:walmart.com"},
    {"site": "Walmart", "query": "laptop MacBook site:walmart.com"},
    {"site": "Walmart", "query": "refrigerator washer dryer site:walmart.com"},
    {"site": "Target", "query": "4K TV OLED QLED site:target.com"},
    {"site": "Target", "query": "PlayStation 5 Xbox Series X site:target.com"},
    {"site": "Target", "query": "refrigerator washer appliance site:target.com"},
    {"site": "Best Buy", "query": "4K TV OLED QLED site:bestbuy.com"},
    {"site": "Best Buy", "query": "PlayStation 5 Xbox Series X site:bestbuy.com"},
    {"site": "Best Buy", "query": "laptop MacBook site:bestbuy.com"},
    {"site": "Best Buy", "query": "refrigerator washer dryer site:bestbuy.com"},
    {"site": "Home Depot", "query": "refrigerator washer dryer site:homedepot.com"},
    {"site": "Amazon", "query": "4K TV OLED QLED site:amazon.com"},
    {"site": "Amazon", "query": "PlayStation 5 Xbox Series X site:amazon.com"},
    {"site": "Amazon", "query": "laptop MacBook site:amazon.com"},
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


def get_urls_from_search(query: str, site: str) -> list[str]:
    """Get product page URLs from a Firecrawl search."""
    try:
        results = firecrawl.search(query, limit=5)
        urls = []
        if isinstance(results, list):
            items = results
        elif hasattr(results, "data"):
            items = results.data
        else:
            items = []
        for r in items:
            url = r.url if hasattr(r, "url") else r.get("url", "") if isinstance(r, dict) else ""
            if url:
                urls.append(url)
        return urls
    except Exception as e:
        log.warning(f"  [{site}] Search failed for '{query}': {e}")
        return []


def scrape_product_page(url: str, site: str) -> list[dict]:
    """Scrape a single product page for name and price."""
    try:
        result = firecrawl.scrape_url(url, formats=["markdown"])
        markdown = result.markdown if hasattr(result, "markdown") else ""
        if not markdown:
            return []

        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Extract the product name and price from this retail page. "
                        "Return ONLY a JSON object with keys: name (string), price (number, no $ sign). "
                        "If you cannot find both a clear product name and price, return {}.\n\n"
                        f"{markdown[:4000]}"
                    ),
                }
            ],
        )
        text = response.content[0].text.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return []
        data = json.loads(match.group())
        if not data.get("name") or not data.get("price"):
            return []
        return [{"name": data["name"], "price": data["price"], "url": url, "site": site}]
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
    url = product.get("url", "No link available")

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
    """Run a full scan across all configured search queries."""
    log.info(f"=== Scan started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    all_products = []

    for target in SEARCH_QUERIES:
        site = target["site"]
        query = target["query"]
        urls = get_urls_from_search(query, site)
        for url in urls:
            products = scrape_product_page(url, site)
            all_products.extend(products)
            if products:
                log.info(f"  [{site}] {products[0]['name']} @ ${products[0]['price']}")

    total_products = len(all_products)
    log.info(f"Total products found: {total_products}")

    total_flagged = 0
    batch_size = 50
    for i in range(0, len(all_products), batch_size):
        batch = all_products[i : i + batch_size]
        flagged = check_prices_with_claude(batch)
        total_flagged += len(flagged)
        for product in flagged:
            log.warning(
                f"PRICE ERROR: {product['name']} @ ${product['price']} on {product['site']} — {product['reason']}"
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
