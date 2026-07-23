"""
Deal Falcon Bot - your personal UAE price-slash hunter.

Checks Amazon.ae and Noon for big discounts and sends Telegram alerts
straight to your phone. Everything you might want to change lives in
config.json - you never need to touch this file.

How pages are fetched:
  Noon    -> real browser (Playwright, FREE on GitHub) first, then any
             scraping API key you have set.
  Amazon  -> scraping API structured reader first (cheap + clean), then any
             provider's plain fetch, then the free browser.
You can run with NO scraping API key at all - the free browser handles it.
Supported keys (add any ONE as a GitHub secret): SCRAPERAPI_KEY,
SCRAPINGBEE_KEY, ZENROWS_KEY, SCRAPEDO_KEY, SCRAPFLY_KEY.
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
IS_MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"

if not BOT_TOKEN or not CHAT_ID:
    sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. "
             "Add them as GitHub secrets (see README step 6).")

MIN_DISCOUNT = float(CONFIG.get("min_discount_percent", 40))
MAX_ALERTS = int(CONFIG.get("max_alerts_per_run", 10))
USE_BROWSER = bool(CONFIG.get("use_free_browser", True))
REMEMBER_DAYS = 7

STATE_FILE = ROOT / "state" / "seen.json"
STATE_FILE.parent.mkdir(exist_ok=True)
try:
    _state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
except Exception:
    _state = {}
if isinstance(_state, dict) and "seen" in _state:
    SEEN = _state.get("seen", {})
    CURSOR = int(_state.get("cursor", 0))
    NOON_CURSOR = int(_state.get("noon_cursor", 0))
else:
    SEEN = _state if isinstance(_state, dict) else {}
    CURSOR = 0
    NOON_CURSOR = 0

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AE,en;q=0.9,ar;q=0.8",
}

ERRORS = []
TRACE = []
_seen_fingerprints = set()


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------- scraping providers

def provider_specs(url, render):
    """Every scraping service the bot understands. Only ones whose key is
    set as a GitHub secret are used, so you can switch provider by simply
    adding a different secret - no code changes."""
    key = os.environ.get("SCRAPERAPI_KEY", "").strip()
    if key:
        yield ("scraperapi", "https://api.scraperapi.com/",
               {"api_key": key, "url": url, "country_code": "ae",
                "render": "true" if render else "false"}, None)

    key = os.environ.get("SCRAPINGBEE_KEY", "").strip()
    if key:
        yield ("scrapingbee", "https://app.scrapingbee.com/api/v1/",
               {"api_key": key, "url": url, "country_code": "ae",
                "render_js": "true" if render else "false"}, None)

    key = os.environ.get("ZENROWS_KEY", "").strip()
    if key:
        p = {"apikey": key, "url": url, "proxy_country": "ae"}
        if render:
            p["js_render"] = "true"
        yield ("zenrows", "https://api.zenrows.com/v1/", p, None)

    key = os.environ.get("SCRAPEDO_KEY", "").strip()
    if key:
        p = {"token": key, "url": url, "geoCode": "ae"}
        if render:
            p["render"] = "true"
        yield ("scrape.do", "https://api.scrape.do/", p, None)

    key = os.environ.get("SCRAPFLY_KEY", "").strip()
    if key:
        p = {"key": key, "url": url, "country": "ae", "asp": "true"}
        if render:
            p["render_js"] = "true"
        yield ("scrapfly", "https://api.scrapfly.io/scrape", p, "scrapfly")


def call_providers(url, render=True):
    """Try each configured provider in turn. Yields (name, html)."""
    for name, endpoint, params, wrapper in provider_specs(url, render):
        try:
            r = requests.get(endpoint, params=params, timeout=240)
            if r.status_code != 200:
                TRACE.append(f"{name}: HTTP {r.status_code}")
                continue
            text = r.text
            if wrapper == "scrapfly":
                text = r.json().get("result", {}).get("content", "")
            if len(text) > 3000:
                yield name, text
        except Exception as e:
            TRACE.append(f"{name}: {type(e).__name__}")
        time.sleep(1)


# ------------------------------------------------- free browser (Playwright)

def browser_fetch(url, scroll=True):
    """Render a page in a real Chromium browser on GitHub's server. Free.

    Returns (html, captured_json). We listen to the site's own background
    data requests, which is far more reliable than reading the visible page,
    because that is where the real product data travels.
    """
    if not USE_BROWSER:
        return None, []
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        TRACE.append("browser: playwright not installed")
        return None, []

    captured = []
    content = None
    try:
        with sync_playwright() as p:
            # Noon rejects Chromium's HTTP/2 connection (ERR_HTTP2_PROTOCOL_
            # ERROR) but happily serves plain HTTP/1.1, so we turn HTTP/2 and
            # QUIC off and let the browser fall back to 1.1.
            browser = p.chromium.launch(
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-gpu",
                      "--disable-http2",
                      "--disable-quic",
                      "--disable-features=UseChromiumHTTP2,AsyncDns"])
            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="en-AE",
                timezone_id="Asia/Dubai",
                viewport={"width": 1366, "height": 900},
                extra_http_headers={"Accept-Language": "en-AE,en;q=0.9"})
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',"
                "{get:()=>undefined})")

            # skip images/fonts/video: we only want data, and heavy assets
            # are the main cause of navigation timeouts
            def block_heavy(route):
                if route.request.resource_type in ("image", "media", "font"):
                    route.abort()
                else:
                    route.continue_()
            ctx.route("**/*", block_heavy)

            page = ctx.new_page()

            def on_response(resp):
                try:
                    if resp.status == 200 and "json" in resp.headers.get(
                            "content-type", "").lower():
                        captured.append(resp.json())
                except Exception:
                    pass

            page.on("response", on_response)

            nav_error = None
            for wait_mode in ("domcontentloaded", "commit"):
                try:
                    resp = page.goto(url, wait_until=wait_mode, timeout=60000)
                    TRACE.append(
                        f"browser: opened, HTTP {resp.status if resp else '?'}")
                    nav_error = None
                    break
                except Exception as e:
                    nav_error = f"{type(e).__name__}: {str(e)[:110]}"
                    time.sleep(3)
            if nav_error:
                TRACE.append("browser: " + nav_error)

            # even after a navigation error the page may hold usable data,
            # so always try to read it rather than giving up
            try:
                page.wait_for_timeout(6000)
                if scroll:
                    for _ in range(3):
                        page.mouse.wheel(0, 5000)
                        page.wait_for_timeout(2500)
                content = page.content()
                if content and len(content) < 2000:
                    TRACE.append(f"browser: tiny page ({len(content)} chars)"
                                 " - likely blocked")
            except Exception as e:
                TRACE.append(f"browser read: {type(e).__name__}: "
                             f"{str(e)[:100]}")
            browser.close()
        return content, captured
    except Exception as e:
        TRACE.append(f"browser: {type(e).__name__}: {str(e)[:150]}")
        return content, captured


# ---------------------------------------------------------------- parsing

def clean_number(value):
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
        for k in ("value", "amount", "price", "current"):
            if k in value:
                return clean_number(value[k])
        return None
    else:
        return None
    return n if 1 <= n <= 1_000_000 else None


SALE_KEYS = ["sale_price", "salePrice", "offer_price", "offerPrice",
             "final_price", "deal_price", "discounted_price",
             "current_price", "special_price"]
GENERIC_PRICE_KEYS = ["price", "amount", "value"]
WAS_KEYS = ["original_price", "originalPrice", "old_price", "oldPrice",
            "list_price", "listPrice", "was_price", "wasPrice",
            "strikethrough_price", "rrp", "msrp", "regular_price"]
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


def read_prices(d):
    """Stores disagree on which key means what, so let the numbers decide:
    the lower value is always what you pay today."""
    sale = pick_number(d, SALE_KEYS)
    generic = pick_number(d, GENERIC_PRICE_KEYS)
    was = pick_number(d, WAS_KEYS)
    candidates = [n for n in (sale, generic, was) if n]
    if len(candidates) < 2:
        return None, None
    now_price = sale if sale else generic
    old_price = was if was else max(candidates)
    if not now_price:
        return None, None
    if old_price <= now_price:
        old_price = max(candidates)
    if old_price <= now_price:
        return None, None
    return now_price, old_price


def add_deal(out, store, name, now, was, url, base_url):
    if not (name and now and was and was > now):
        return
    if url and url.startswith("/"):
        url = base_url.rstrip("/") + url
    if not url or not url.startswith("http"):
        q = urllib.parse.quote_plus(name[:80])
        url = (f"https://www.amazon.ae/s?k={q}" if store == "Amazon.ae"
               else f"https://www.noon.com/uae-en/search/?q={q}")
    pct = round((was - now) / was * 100)
    fp = (store, name.lower()[:60], round(now, 2))
    if fp in _seen_fingerprints:
        return
    _seen_fingerprints.add(fp)
    out.append({"store": store, "name": name, "now": now, "was": was,
                "pct": pct, "url": url})


def harvest(obj, store, base_url, out):
    if isinstance(obj, dict):
        now, was = read_prices(obj)
        add_deal(out, store, pick_text(obj, NAME_KEYS), now, was,
                 pick_text(obj, URL_KEYS) or "", base_url)
        for v in obj.values():
            harvest(v, store, base_url, out)
    elif isinstance(obj, list):
        for v in obj:
            harvest(v, store, base_url, out)


def json_blobs(html_text):
    blobs = re.findall(
        r'<script[^>]*type="application/(?:json|ld\+json)"[^>]*>(.*?)</script>',
        html_text, re.DOTALL)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                  html_text, re.DOTALL)
    if m:
        blobs.append(m.group(1))
    chunks = re.findall(
        r'self\.__next_f\.push\(\[\d+\s*,\s*"((?:[^"\\]|\\.)*)"', html_text)
    if chunks:
        try:
            blobs.append("".join(json.loads('"' + c + '"') for c in chunks))
        except Exception:
            pass
    return blobs


def scan_objects(text, store, base_url, out):
    """Find product-shaped JSON objects anywhere in a blob of text."""
    if len(text) > 6_000_000:
        text = text[:6_000_000]
    before = len(out)
    for m in re.finditer(r'"(?:price|sale_price|salePrice)"\s*:', text):
        start = text.rfind("{", max(0, m.start() - 4000), m.start())
        if start == -1:
            continue
        depth, end, in_str, esc = 0, -1, False, False
        for i in range(start, min(len(text), start + 8000)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            continue
        try:
            harvest(json.loads(text[start:end]), store, base_url, out)
        except Exception:
            continue
        if len(out) - before > 400:
            break


def parse_page(html_text, store, base_url):
    items = []
    for blob in json_blobs(html_text):
        try:
            harvest(json.loads(blob), store, base_url, items)
        except Exception:
            scan_objects(blob, store, base_url, items)
    if not items:
        scan_objects(html_text, store, base_url, items)
    return items


def parse_amazon_html(html_text):
    """Amazon keeps prices in HTML, not JSON, so read the result cards."""
    out = []
    for block in html_text.split('data-asin="')[1:]:
        asin = block[:15].split('"')[0]
        chunk = block[:7000]
        m = (re.search(r'<h2[^>]*aria-label="([^"]{5,250})"', chunk)
             or re.search(r'<h2[^>]*>.*?<span[^>]*>([^<]{5,250})</span>',
                          chunk, re.DOTALL))
        if not m:
            continue
        name = html.unescape(m.group(1)).strip()
        prices = [clean_number(p) for p in re.findall(
            r'<span class="a-offscreen">\s*(?:AED)?\s*([\d,]+\.?\d*)', chunk)]
        prices = [p for p in prices if p][:3]
        if len(prices) < 2:
            continue
        url = f"https://www.amazon.ae/dp/{asin}" if asin else ""
        add_deal(out, "Amazon.ae", name, min(prices), max(prices), url,
                 "https://www.amazon.ae")
    return out


# ---------------------------------------------------------------- stores

def check_amazon(queries):
    found = []
    sapi = os.environ.get("SCRAPERAPI_KEY", "").strip()
    for query in queries:
        got = []
        url = "https://www.amazon.ae/s?k=" + urllib.parse.quote_plus(query)
        # 1. ScraperAPI's ready-made Amazon reader (cheapest, cleanest)
        if sapi:
            try:
                r = requests.get(
                    "https://api.scraperapi.com/structured/amazon/search",
                    params={"api_key": sapi, "query": query,
                            "tld": "ae", "country": "ae"}, timeout=120)
                if r.status_code == 200:
                    harvest(r.json(), "Amazon.ae",
                            "https://www.amazon.ae", got)
            except Exception as e:
                TRACE.append(f"amazon structured: {type(e).__name__}")
        # 2. any other provider, reading the normal search page
        if not got:
            for name, page in call_providers(url, render=False):
                got = parse_amazon_html(page)
                if got:
                    TRACE.append(f"amazon '{query}': {len(got)} via {name}")
                    break
        # 3. free browser as the last resort
        if not got:
            page, blobs = browser_fetch(url, scroll=True)
            if page:
                got = parse_amazon_html(page)
                for b in blobs:
                    harvest(b, "Amazon.ae", "https://www.amazon.ae", got)
                if got:
                    TRACE.append(f"amazon '{query}': {len(got)} via browser")
        if not got:
            TRACE.append(f"amazon '{query}': 0 found")
        found += got
        time.sleep(2)
    return found


def check_noon(queries):
    found = []
    for query in queries:
        url = ("https://www.noon.com/uae-en/search/?q="
               + urllib.parse.quote_plus(query))
        got = []
        # 1. free real browser, capturing Noon's own data requests
        page, blobs = browser_fetch(url, scroll=True)
        for b in blobs:
            harvest(b, "Noon", "https://www.noon.com", got)
        if not got and page:
            got = parse_page(page, "Noon", "https://www.noon.com")
        if got:
            TRACE.append(f"noon '{query}': {len(got)} via browser")
        # 2. paid providers, fully rendered
        if not got:
            for name, page in call_providers(url, render=True):
                got = parse_page(page, "Noon", "https://www.noon.com")
                if got:
                    TRACE.append(f"noon '{query}': {len(got)} via {name}")
                    break
        if not got:
            TRACE.append(f"noon '{query}': 0 found")
        found += got
        time.sleep(2)
    return found


# ---------------------------------------------------------------- rotation

def rotate(terms, per_run, cursor):
    """Return this run's slice of the keyword list, plus where to resume."""
    terms = [t for t in terms if t and t.strip()]
    if not terms:
        return [], 0
    if per_run <= 0 or per_run >= len(terms):
        return terms, 0
    start = cursor % len(terms)
    doubled = terms + terms
    return doubled[start:start + per_run], (start + per_run) % len(terms)


# ---------------------------------------------------------------- telegram

def send_telegram(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}, timeout=30)
        if r.status_code != 200:
            log(f"Telegram error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log(f"Telegram send failed: {e}")


# ---------------------------------------------------------------- main

def main():
    a_terms = CONFIG.get("amazon_searches", [])
    n_terms = CONFIG.get("noon_searches", [])
    a_queries, a_next = rotate(
        a_terms, int(CONFIG.get("searches_per_run", 10)), CURSOR)
    n_queries, n_next = rotate(
        n_terms, int(CONFIG.get("noon_searches_per_run", 4)), NOON_CURSOR)
    log(f"Amazon slice ({len(a_queries)}/{len(a_terms)}): {a_queries}")
    log(f"Noon slice ({len(n_queries)}/{len(n_terms)}): {n_queries}")

    deals = check_amazon(a_queries) + check_noon(n_queries)
    log(f"Discounted products found: {len(deals)}")

    hot = [d for d in deals if d["pct"] >= MIN_DISCOUNT]
    now_ts = time.time()
    fresh = []
    for d in sorted(hot, key=lambda x: -x["pct"]):
        key = f"{d['store']}|{d['name'][:60].lower()}|{int(d['now'])}"
        if now_ts - SEEN.get(key, 0) < REMEMBER_DAYS * 86400:
            continue
        fresh.append((key, d))

    alerts = fresh[:MAX_ALERTS]
    log(f"Deals >= {MIN_DISCOUNT:.0f}% off, new this week: {len(fresh)}")

    if alerts:
        lines = []
        for key, d in alerts:
            SEEN[key] = now_ts
            lines.append(
                f"\U0001F525 <b>\u2212{d['pct']}%</b> \u00b7 {d['store']}\n"
                f"{html.escape(d['name'][:90])}\n"
                f"<b>AED {d['now']:,.0f}</b>  <s>AED {d['was']:,.0f}</s>"
                f"  (save AED {d['was'] - d['now']:,.0f})\n"
                f"<a href=\"{html.escape(d['url'], quote=True)}\">"
                f"Open product \u2197</a>")
        chunk = (f"\U0001F985 <b>Deal Falcon</b> \u2014 {len(alerts)} slash"
                 f"{'es' if len(alerts) > 1 else ''} \u2265 "
                 f"{MIN_DISCOUNT:.0f}% off\n\n")
        for line in lines:
            if len(chunk) + len(line) > 3500:
                send_telegram(chunk)
                chunk = ""
            chunk += line + "\n\n"
        if chunk.strip():
            send_telegram(chunk)

    for k in [k for k, ts in SEEN.items()
              if now_ts - ts > REMEMBER_DAYS * 86400]:
        del SEEN[k]
    STATE_FILE.write_text(
        json.dumps({"cursor": a_next, "noon_cursor": n_next, "seen": SEEN},
                   indent=0), encoding="utf-8")

    if IS_MANUAL_RUN:
        status = (f"\u2705 Deal Falcon is alive.\n"
                  f"Amazon: {len(a_queries)}/{len(a_terms)} searches \u00b7 "
                  f"Noon: {len(n_queries)}/{len(n_terms)} searches\n"
                  f"Found {len(deals)} discounted products \u00b7 "
                  f"{len(fresh)} passed your \u2265{MIN_DISCOUNT:.0f}% rule.")
        if TRACE:
            status += "\n\n\U0001F50D Trace:\n" + "\n".join(
                "\u2022 " + html.escape(t) for t in TRACE[:14])
        if ERRORS:
            status += "\n\n\u26A0\uFE0F Notes:\n" + "\n".join(
                "\u2022 " + html.escape(e) for e in ERRORS[:5])
        send_telegram(status)

    for t in TRACE:
        log("TRACE: " + t)


if __name__ == "__main__":
    main()
