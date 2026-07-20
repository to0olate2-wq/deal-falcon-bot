"""
Deal Falcon Bot - your personal UAE price-slash hunter.

Checks Amazon.ae and Noon for big discounts and sends Telegram alerts
straight to your phone. Everything you might want to change lives in
config.json - you never need to touch this file.
"""

import html
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

# ---------------------------------------------------------------- setup

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SCRAPER_KEY = os.environ.get("SCRAPERAPI_KEY", "").strip()
IS_MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"

if not BOT_TOKEN or not CHAT_ID:
    sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. "
             "Add them as GitHub secrets (see README step 6).")

MIN_DISCOUNT = float(CONFIG.get("min_discount_percent", 40))
MAX_ALERTS = int(CONFIG.get("max_alerts_per_run", 10))
REMEMBER_DAYS = 7  # don't re-alert the same deal within this many days

STATE_FILE = ROOT / "state" / "seen.json"
STATE_FILE.parent.mkdir(exist_ok=True)
try:
    SEEN = json.loads(STATE_FILE.read_text(encoding="utf-8"))
except Exception:
    SEEN = {}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AE,en;q=0.9,ar;q=0.8",
}

ERRORS = []

# ---------------------------------------------------------------- helpers

def log(msg):
    print(msg, flush=True)


def send_telegram(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if r.status_code != 200:
            log(f"Telegram error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log(f"Telegram send failed: {e}")


def clean_number(value):
    """Turn 'AED 1,299.00', 1299, '1299' into a float. Returns None if it can't."""
    if isinstance(value, (int, float)):
        n = float(value)
    elif isinstance(value, str):
        cleaned = re.sub(r"[^0-9.]", "", value.replace(",", ""))
        if not cleaned or cleaned.count(".") > 1:
            return None
        try:
            n = float(cleaned)
        except ValueError:
            return None
    elif isinstance(value, dict):
        # some sites nest prices like {"value": 1299} or {"amount": "1299"}
        for k in ("value", "amount", "price", "current"):
            if k in value:
                return clean_number(value[k])
        return None
    else:
        return None
    return n if 1 <= n <= 1_000_000 else None


NOW_KEYS = ["price", "current_price", "sale_price", "salePrice",
            "offer_price", "offerPrice", "final_price", "deal_price"]
WAS_KEYS = ["original_price", "originalPrice", "old_price", "oldPrice",
            "list_price", "listPrice", "was_price", "wasPrice",
            "strikethrough_price", "rrp", "msrp"]
NAME_KEYS = ["name", "title", "product_name", "productName", "product_title"]
URL_KEYS = ["url", "link", "product_url", "productUrl", "href"]


def pick_number(d, keys):
    for k in keys:
        if k in d:
            n = clean_number(d[k])
            if n:
                return n
    return None


def pick_text(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and 3 <= len(v.strip()) <= 300:
            return v.strip()
    return None


def harvest(obj, store, base_url, out):
    """
    Walk any JSON structure and pull out everything that looks like a
    discounted product: it has a name, a current price, and a higher old price.
    This works without knowing the exact page layout, so it survives redesigns
    better than fixed rules.
    """
    if isinstance(obj, dict):
        now = pick_number(obj, NOW_KEYS)
        was = pick_number(obj, WAS_KEYS)
        name = pick_text(obj, NAME_KEYS)
        if name and now and was and was > now:
            url = pick_text(obj, URL_KEYS) or ""
            if url and url.startswith("/"):
                url = base_url.rstrip("/") + url
            if not url.startswith("http"):
                # guaranteed-useful fallback: link to a search for this product
                q = urllib.parse.quote_plus(name[:80])
                if store == "Amazon.ae":
                    url = f"https://www.amazon.ae/s?k={q}"
                else:
                    url = f"https://www.noon.com/uae-en/search/?q={q}"
            pct = round((was - now) / was * 100)
            out.append({
                "store": store, "name": name, "now": now,
                "was": was, "pct": pct, "url": url,
            })
        for v in obj.values():
            harvest(v, store, base_url, out)
    elif isinstance(obj, list):
        for v in obj:
            harvest(v, store, base_url, out)


# ---------------------------------------------------------------- amazon.ae

def check_amazon():
    """Uses ScraperAPI's structured Amazon endpoint - returns clean product
    data with current price and original price, no fragile page-parsing."""
    found = []
    if not SCRAPER_KEY:
        ERRORS.append("Amazon skipped: no SCRAPERAPI_KEY secret set.")
        return found
    for query in CONFIG.get("amazon_searches", []):
        try:
            r = requests.get(
                "https://api.scraperapi.com/structured/amazon/search",
                params={
                    "api_key": SCRAPER_KEY,
                    "query": query,
                    "tld": "ae",
                    "country": "ae",
                },
                timeout=120,
            )
            if r.status_code != 200:
                ERRORS.append(f"Amazon '{query}': HTTP {r.status_code}")
                continue
            harvest(r.json(), "Amazon.ae", "https://www.amazon.ae", found)
            log(f"Amazon '{query}': ok")
        except Exception as e:
            ERRORS.append(f"Amazon '{query}': {type(e).__name__}")
        time.sleep(2)
    return found


# ---------------------------------------------------------------- noon

def fetch_page(url):
    """Try fetching directly (free). If Noon blocks the request and a
    ScraperAPI key exists, retry through ScraperAPI."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        if r.status_code == 200 and "captcha" not in r.text[:3000].lower():
            return r.text
    except Exception:
        pass
    if SCRAPER_KEY:
        try:
            r = requests.get(
                "https://api.scraperapi.com/",
                params={"api_key": SCRAPER_KEY, "url": url,
                        "country_code": "ae", "render": "true"},
                timeout=180,
            )
            if r.status_code == 200:
                return r.text
            ERRORS.append(f"Noon via ScraperAPI: HTTP {r.status_code}")
        except Exception as e:
            ERRORS.append(f"Noon via ScraperAPI: {type(e).__name__}")
    else:
        ERRORS.append("Noon blocked the direct request and no SCRAPERAPI_KEY "
                      "is set to retry through.")
    return None


def check_noon():
    found = []
    for page_url in CONFIG.get("noon_pages", []):
        html_text = fetch_page(page_url)
        if not html_text:
            continue
        # Noon embeds its product data as JSON inside the page. Grab every
        # JSON blob we can find and harvest products from all of them.
        blobs = re.findall(
            r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
            html_text, re.DOTALL)
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html_text, re.DOTALL)
        if m:
            blobs.append(m.group(1))
        parsed_any = False
        for blob in blobs:
            try:
                data = json.loads(blob)
            except Exception:
                continue
            harvest(data, "Noon", "https://www.noon.com", found)
            parsed_any = True
        if parsed_any:
            log(f"Noon page ok: {page_url}")
        else:
            ERRORS.append(f"Noon: couldn't read product data on {page_url}")
        time.sleep(2)
    return found


# ---------------------------------------------------------------- main

def main():
    deals = check_amazon() + check_noon()
    log(f"Raw products with a discount found: {len(deals)}")

    # keep only deals that meet YOUR threshold
    hot = [d for d in deals if d["pct"] >= MIN_DISCOUNT]

    # de-duplicate within this run and against recent alerts
    now_ts = time.time()
    fresh, seen_keys = [], set()
    for d in sorted(hot, key=lambda x: -x["pct"]):
        key = f"{d['store']}|{d['name'][:60].lower()}|{int(d['now'])}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        last = SEEN.get(key, 0)
        if now_ts - last < REMEMBER_DAYS * 86400:
            continue
        fresh.append((key, d))

    alerts = fresh[:MAX_ALERTS]
    log(f"Deals >= {MIN_DISCOUNT:.0f}% off, new this week: {len(fresh)} "
        f"(alerting top {len(alerts)})")

    if alerts:
        lines = []
        for key, d in alerts:
            SEEN[key] = now_ts
            name = html.escape(d["name"][:90])
            lines.append(
                f"\U0001F525 <b>\u2212{d['pct']}%</b> \u00b7 {d['store']}\n"
                f"{name}\n"
                f"<b>AED {d['now']:,.0f}</b>  <s>AED {d['was']:,.0f}</s>"
                f"  (save AED {d['was'] - d['now']:,.0f})\n"
                f"<a href=\"{d['url']}\">Open product \u2197</a>"
            )
        header = (f"\U0001F985 <b>Deal Falcon</b> \u2014 {len(alerts)} slash"
                  f"{'es' if len(alerts) > 1 else ''} \u2265 "
                  f"{MIN_DISCOUNT:.0f}% off\n\n")
        chunk = header
        for line in lines:
            if len(chunk) + len(line) > 3500:
                send_telegram(chunk)
                chunk = ""
            chunk += line + "\n\n"
        if chunk.strip():
            send_telegram(chunk)

    # prune old memory
    for k in [k for k, ts in SEEN.items() if now_ts - ts > REMEMBER_DAYS * 86400]:
        del SEEN[k]
    STATE_FILE.write_text(json.dumps(SEEN, indent=0), encoding="utf-8")

    # on a manual test run, always confirm the bot is alive
    if IS_MANUAL_RUN:
        status = (f"\u2705 Deal Falcon is alive.\n"
                  f"Scanned Amazon.ae + Noon \u00b7 found {len(deals)} "
                  f"discounted products \u00b7 {len(fresh)} passed your "
                  f"\u2265{MIN_DISCOUNT:.0f}% rule.")
        if ERRORS:
            status += "\n\n\u26A0\uFE0F Notes:\n" + "\n".join(
                "\u2022 " + html.escape(e) for e in ERRORS[:6])
        send_telegram(status)

    for e in ERRORS:
        log("WARNING: " + e)


if __name__ == "__main__":
    main()
