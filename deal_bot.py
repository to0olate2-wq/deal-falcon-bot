"""
Deal Falcon Bot - your personal Amazon.ae price-slash hunter.

Scans Amazon.ae for big discounts and sends Telegram alerts to your phone.

You never need to edit this file:
  * settings live in config.json
  * or change them from your phone by messaging your bot (send /help)

Needs ONE scraping key as a GitHub secret. Any of these works:
  SCRAPERAPI_KEY, SCRAPINGBEE_KEY, ZENROWS_KEY, SCRAPEDO_KEY, SCRAPFLY_KEY
"""

import hashlib
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
COMMANDS_ONLY = "--commands-only" in sys.argv

if not BOT_TOKEN or not CHAT_ID:
    sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret.")

# CHAT_ID is you: settings replies and error notices always go here privately.
# post_to in config.json is where the DEALS go - set it to a public channel
# (e.g. "@MyUaeDeals") and anyone who joins that channel sees the deals.
# Leave it empty and deals come to you privately, as before.
def post_target():
    """Where DEALS go. A /post command wins, then config.json, then you."""
    v = STATE["settings"].get("post_to")
    if v is None:
        v = CONFIG.get("post_to")
    v = str(v or "").strip()
    return v or CHAT_ID


def posting_elsewhere():
    return str(post_target()) != str(CHAT_ID)

DEFAULTS = {
    "min_discount_percent": 30,
    "max_discount_percent": 85,
    "searches_per_run": 999,
    "max_alerts_per_run": 25,
}
REMEMBER_DAYS = 7

# ---------------------------------------------------------------- state

STATE_FILE = ROOT / "state" / "seen.json"
STATE_FILE.parent.mkdir(exist_ok=True)


def load_state():
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    # migrate older layouts without losing the deal memory
    if "seen" not in raw:
        raw = {"seen": raw if raw else {}}
    raw.setdefault("seen", {})
    raw.setdefault("settings", {})
    raw.setdefault("cursor", 0)
    raw.setdefault("last_update_id", 0)
    raw.setdefault("paused", False)
    raw.setdefault("known_chats", {})
    # keys that ran out of credits, stored as hashes - never the real key,
    # because this file is committed to your repository
    raw.setdefault("dead_keys", {})
    return raw


STATE = load_state()


def setting(key):
    """Telegram overrides win, then config.json, then the built-in default."""
    v = STATE["settings"].get(key)
    if v is None:
        v = CONFIG.get(key)
    if v is None:
        v = DEFAULTS[key]
    return float(v)


def save_state():
    STATE_FILE.write_text(json.dumps(STATE, indent=0), encoding="utf-8")


HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "en-AE,en;q=0.9",
}

TRACE = []
SOURCES = {}
# browser rendering costs about 10x a plain fetch, so cap how many times
# per run we fall back to it
BROWSER_BUDGET = [int(CONFIG.get("browser_retries_per_run", 25))]
_fingerprints = set()


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- telegram

_WARNED_OWNER = False


def _post(target, text):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": target, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}, timeout=30)
    return r


def _warn_owner(target, detail):
    """If the channel rejects us, tell the owner privately - once."""
    global _WARNED_OWNER
    if _WARNED_OWNER:
        return
    _WARNED_OWNER = True
    try:
        _post(CHAT_ID,
              f"\u26A0\uFE0F Could not post to <b>{html.escape(str(target))}"
              "</b>.\nCheck that the channel exists and that this bot is an "
              "<b>administrator</b> of it with permission to post messages."
              f"\n\n<code>{html.escape(detail[:200])}</code>")
    except Exception:
        pass


def send_telegram(text, to=None, warn=True):
    """Deals go to POST_TO (your public channel, if set). Settings replies
    and warnings go to the owner by passing to=CHAT_ID."""
    target = to or post_target()
    detail = ""
    try:
        r = _post(target, text)
        if r.status_code == 200:
            return True
        detail = f"HTTP {r.status_code}: {r.text[:180]}"
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
    log(f"Telegram send to {target} failed - {detail}")
    if warn and str(target) != str(CHAT_ID):
        _warn_owner(target, detail)
    return False


HELP_TEXT = (
    "\U0001F985 <b>Deal Falcon commands</b>\n\n"
    "<b>/status</b> - show current settings\n"
    "<b>/discount 35</b> - only alert me at 35% off or more\n"
    "<b>/max 90</b> - ignore discounts above 90% (usually data errors)\n"
    "<b>/alerts 30</b> - most deals in one message\n"
    "<b>/categories 40</b> - categories per sweep (999 = all)\n"
    "<b>/pause</b> - stop hunting\n"
    "<b>/resume</b> - start hunting again\n"
    "<b>/post -100123...</b> - post deals to a channel instead\n"
    "<b>/private</b> - bring deals back to this chat\n"
    "<b>/filter on</b> / <b>off</b> - ask Amazon for discounted items only\n"
    "<b>/forget</b> - clear the memory of deals already sent\n"
    "<b>/help</b> - this list"
)


def last_run_line():
    lr = STATE.get("last_run")
    if not lr:
        return ""
    ago = max(0, int((time.time() - lr["time"]) // 60))
    when = f"{ago} min ago" if ago < 120 else f"{ago // 60} h ago"
    line = (f"Last sweep: <b>{when}</b> \u00b7 {lr['deals']} discounted, "
            f"{lr['new']} new \u00b7 ~{lr['credits']} credits\n")
    fc = credit_forecast(lr["credits"])
    if fc:
        line += re.sub(r"^\U0001F4CA ", "", fc) + "\n"
    return line


def status_text():
    total = len([t for t in CONFIG.get("amazon_searches", []) if t.strip()])
    per = int(setting("searches_per_run"))
    per_shown = "all" if per >= total else str(per)
    return (
        "\u2699\uFE0F <b>Current settings</b>\n"
        f"Alert from: <b>{setting('min_discount_percent'):.0f}%</b> off\n"
        f"Ignore above: <b>{setting('max_discount_percent'):.0f}%</b>\n"
        f"Max alerts per message: <b>{int(setting('max_alerts_per_run'))}</b>\n"
        f"Categories per sweep: <b>{per_shown}</b> (of {total})\n"
        f"Hunting: <b>{'paused' if STATE['paused'] else 'active'}</b>\n"
        f"Deals remembered: <b>{len(STATE['seen'])}</b>\n"
        f"Discount filter: <b>{'on' if filter_on() else 'off'}</b>\n"
        + last_run_line()
        + f"Deals posted to: <b>"
        + (html.escape(str(post_target())) if posting_elsewhere()
           else "this chat") + "</b>"
    )


def set_number(key, raw, low, high, label):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return f"\u274C Give me a number, like <code>/{label} 35</code>"
    if not (low <= v <= high):
        return f"\u274C Pick a number between {low:.0f} and {high:.0f}."
    STATE["settings"][key] = v
    return None


def apply_command(text):
    """Handle one /command and return the reply to send back."""
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else None

    if cmd in ("/help", "/start"):
        return HELP_TEXT
    if cmd == "/status":
        return status_text()

    if cmd == "/discount":
        err = set_number("min_discount_percent", arg, 1, 99, "discount")
        if err:
            return err
        lo = setting("min_discount_percent")
        if lo >= setting("max_discount_percent"):
            STATE["settings"]["max_discount_percent"] = min(99.0, lo + 10)
        return (f"\u2705 Now alerting from <b>{lo:.0f}%</b> off.\n\n"
                + status_text())

    if cmd == "/max":
        err = set_number("max_discount_percent", arg, 2, 100, "max")
        if err:
            return err
        hi = setting("max_discount_percent")
        if hi <= setting("min_discount_percent"):
            STATE["settings"]["min_discount_percent"] = max(1.0, hi - 10)
        return f"\u2705 Ignoring anything above <b>{hi:.0f}%</b> off."

    if cmd == "/alerts":
        err = set_number("max_alerts_per_run", arg, 1, 100, "alerts")
        if err:
            return err
        return (f"\u2705 Up to <b>{int(setting('max_alerts_per_run'))}</b> "
                "deals per message.")

    if cmd == "/categories":
        err = set_number("searches_per_run", arg, 1, 999, "categories")
        if err:
            return err
        total = len([t for t in CONFIG.get("amazon_searches", []) if t.strip()])
        per = int(setting("searches_per_run"))
        if per >= total:
            return (f"\u2705 Scanning <b>all {total}</b> categories every "
                    "sweep.")
        runs = -(-total // per)
        return (f"\u2705 <b>{per}</b> categories per sweep - the full list is "
                f"covered every {runs} sweeps.")

    if cmd == "/post":
        if not arg:
            return ("Send the channel ID after the command, like\n"
                    "<code>/post -1001234567890</code>\n\n"
                    "Add me as an administrator of your channel and I'll "
                    "message you its ID automatically.")
        had = "post_to" in STATE["settings"]
        old = STATE["settings"].get("post_to")
        STATE["settings"]["post_to"] = arg
        if send_telegram("\U0001F985 Deal Falcon will post deals here.",
                         to=arg, warn=False):
            return (f"\u2705 Deals will now go to <b>{html.escape(arg)}</b>.\n"
                    "Settings replies stay here with you.\n"
                    "Send /private to bring deals back to this chat.")
        if had:
            STATE["settings"]["post_to"] = old
        else:
            STATE["settings"].pop("post_to", None)
        return ("\u274C I couldn't post there. Check that I'm an "
                "<b>administrator</b> of that channel with permission to post "
                "messages, then send the command again.")

    if cmd == "/private":
        STATE["settings"]["post_to"] = ""
        return "\u2705 Deals will come to this chat only."

    if cmd == "/filter":
        if arg not in ("on", "off"):
            return ("Send <code>/filter on</code> or <code>/filter off</code>."
                    "\nOn asks Amazon for discounted items only.")
        STATE["settings"]["use_discount_filter"] = (arg == "on")
        return f"\u2705 Discount filter turned <b>{arg}</b>."

    if cmd == "/pause":
        STATE["paused"] = True
        return "\u23F8 Hunting paused. Send /resume to start again."
    if cmd == "/resume":
        STATE["paused"] = False
        return "\u25B6\uFE0F Hunting resumed."

    if cmd == "/forget":
        n = len(STATE["seen"])
        STATE["seen"] = {}
        return (f"\U0001F9F9 Forgot {n} remembered deals - you may see some "
                "repeats on the next sweep.")

    return "\u2753 Unknown command. Send /help for the list."


def announce_chat(chat):
    """Tell the owner privately about a channel the bot can now post to."""
    cid = str(chat.get("id", ""))
    if not cid or cid in STATE["known_chats"]:
        return
    STATE["known_chats"][cid] = chat.get("title", "")
    title = html.escape(chat.get("title") or "your channel")
    send_telegram(
        f"\U0001F4E3 I've been added to <b>{title}</b>.\n"
        f"Its ID is <code>{html.escape(cid)}</code>\n\n"
        f"To send deals there, reply:\n<code>/post {html.escape(cid)}</code>",
        to=CHAT_ID)


def read_commands():
    """Read any /commands you sent since the last run and apply them."""
    offset = int(STATE.get("last_update_id", 0)) + 1
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                         params={"offset": offset, "timeout": 0, "limit": 50},
                         timeout=30)
        data = r.json()
    except Exception as e:
        TRACE.append(f"commands: {type(e).__name__}")
        return 0
    if not data.get("ok"):
        TRACE.append("commands: could not read messages")
        return 0

    handled = 0
    for upd in data.get("result", []):
        STATE["last_update_id"] = max(int(STATE.get("last_update_id", 0)),
                                      int(upd.get("update_id", 0)))
        # if I've just been added to a channel or group, tell the owner its
        # ID so they never have to hunt for it
        member = upd.get("my_chat_member") or {}
        post = upd.get("channel_post") or {}
        found = member.get("chat") or post.get("chat") or {}
        if found.get("type") in ("channel", "group", "supergroup"):
            announce_chat(found)
            continue

        msg = upd.get("message") or upd.get("edited_message") or {}
        if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
            continue
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        reply = apply_command(text)
        if reply:
            send_telegram(reply, to=CHAT_ID)
        handled += 1
    if handled:
        TRACE.append(f"applied {handled} command(s)")
    return handled


# ---------------------------------------------------- scraping providers

KEY_NAMES = {
    "SCRAPERAPI_KEY": "scraperapi",
    "SCRAPINGBEE_KEY": "scrapingbee",
    "ZENROWS_KEY": "zenrows",
    "SCRAPEDO_KEY": "scrape.do",
    "SCRAPINGANT_KEY": "scrapingant",
    "SCRAPFLY_KEY": "scrapfly",
}

# a key that runs out of credits is rested this long before being tried again
REST_HOURS = 24
# status codes that mean "this key can't be used right now" - move to the next
KEY_DEAD_CODES = (401, 402, 403)      # wrong key, or out of credits
KEY_BUSY_CODES = (409, 429)           # too many requests at once

_rotation = {}
KEY_USE = {}          # (provider, key number) -> credits spent this run
KEY_STATUS = {}       # (provider, key number) -> "ok" / "out of credits"


def key_list(env_name):
    """One secret can hold several keys, separated by commas or spaces."""
    raw = os.environ.get(env_name, "")
    return [k.strip() for k in re.split(r"[,;\s]+", raw) if k.strip()]


def key_id(key):
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def key_is_resting(key):
    since = STATE["dead_keys"].get(key_id(key))
    return bool(since) and time.time() - since < REST_HOURS * 3600


def keys_in_turn(env_name):
    """Usable keys for a provider, rotated so every account shares the load.
    Returns (key number, key) pairs - the number is only for reports, so no
    part of a real key ever appears in logs."""
    keys = list(enumerate(key_list(env_name), start=1))
    ready = [(i, k) for i, k in keys if not key_is_resting(k)]
    for i, k in keys:
        if key_is_resting(k):
            KEY_STATUS[(KEY_NAMES[env_name], i)] = "out of credits"
    if not ready:
        return []
    start = _rotation.get(env_name, 0) % len(ready)
    _rotation[env_name] = start + 1
    return ready[start:] + ready[:start]


def provider_specs(url):
    """Every service the bot understands, as (name, endpoint, params-maker,
    wrapper, secret name, credit cost). Only services with a key are used."""
    yield ("scraperapi", "https://api.scraperapi.com/",
           lambda k: {"api_key": k, "url": url, "country_code": "ae"},
           None, "SCRAPERAPI_KEY", 1)
    yield ("scrapingbee", "https://app.scrapingbee.com/api/v1/",
           lambda k: {"api_key": k, "url": url, "country_code": "ae",
                      "render_js": "false"},
           None, "SCRAPINGBEE_KEY", 1)
    yield ("zenrows", "https://api.zenrows.com/v1/",
           lambda k: {"apikey": k, "url": url, "proxy_country": "ae"},
           None, "ZENROWS_KEY", 1)
    yield ("scrape.do", "https://api.scrape.do/",
           lambda k: {"token": k, "url": url, "geoCode": "ae"},
           None, "SCRAPEDO_KEY", 1)
    # cheap first: no browser rendering costs 1 credit instead of 10,
    # and Amazon search pages carry prices in plain HTML anyway
    yield ("scrapingant", "https://api.scrapingant.com/v2/general",
           lambda k: {"x-api-key": k, "url": url, "browser": "false",
                      "proxy_country": "ae"},
           None, "SCRAPINGANT_KEY", 1)
    # only reached if the cheap attempt found nothing, and capped per run
    if key_list("SCRAPINGANT_KEY") and BROWSER_BUDGET[0] > 0:
        BROWSER_BUDGET[0] -= 1
        yield ("scrapingant+browser", "https://api.scrapingant.com/v2/general",
               lambda k: {"x-api-key": k, "url": url, "browser": "true"},
               None, "SCRAPINGANT_KEY", 10)
    yield ("scrapfly", "https://api.scrapfly.io/scrape",
           lambda k: {"key": k, "url": url, "country": "ae", "asp": "true"},
           "scrapfly", "SCRAPFLY_KEY", 1)


def keys_detected():
    return [v for k, v in KEY_NAMES.items() if key_list(k)]


def any_key_set():
    return bool(keys_detected())


def fetch_with_keys(env_name, name, endpoint, make_params, cost, timeout=180):
    """Try this provider's keys in turn. A key that is out of credits is set
    aside and the SAME request moves straight to the next key, so an empty
    account never costs you a category. Returns the response, or None."""
    provider = KEY_NAMES[env_name]
    for num, key in keys_in_turn(env_name):
        try:
            r = requests.get(endpoint, params=make_params(key), timeout=timeout)
        except Exception as e:
            TRACE.append(f"{name}: {type(e).__name__}")
            return None                       # network trouble, not the key
        if r.status_code in KEY_DEAD_CODES:
            STATE["dead_keys"][key_id(key)] = time.time()
            KEY_STATUS[(provider, num)] = "out of credits"
            TRACE.append(f"{provider} key {num}: HTTP {r.status_code} - "
                         "out of credits or invalid, switching key")
            continue
        if r.status_code in KEY_BUSY_CODES:
            TRACE.append(f"{provider} key {num}: busy, switching key")
            continue
        KEY_STATUS.setdefault((provider, num), "ok")
        if r.status_code == 200:
            KEY_USE[(provider, num)] = KEY_USE.get((provider, num), 0) + cost
            # a key that works again after its reset is no longer resting
            STATE["dead_keys"].pop(key_id(key), None)
            KEY_STATUS[(provider, num)] = "ok"
        return r                              # a site-side answer: no other
    return None                               # key would do better


def schedule_runs_per_day():
    """Read the hunt schedule from the workflow file, so the forecast always
    matches what GitHub will actually run."""
    try:
        wf = (ROOT / ".github" / "workflows" / "dealbot.yml").read_text()
    except Exception:
        return None
    hours = set()
    for expr in re.findall(r'cron:\s*["\']([^"\']+)["\']', wf):
        fields = expr.split()
        if len(fields) < 2:
            continue
        for part in fields[1].split(","):
            step = 1
            if "/" in part:
                part, step_s = part.split("/", 1)
                step = max(1, int(step_s))
            if part == "*":
                lo, hi = 0, 23
            elif "-" in part:
                lo, hi = (int(x) for x in part.split("-", 1))
            else:
                lo = hi = int(part)
                if step > 1:
                    hi = 23
            hours.update(range(lo, hi + 1, step))
    return len(hours) or None


def credit_forecast(credits_this_run):
    runs = schedule_runs_per_day()
    keys = sum(len(key_list(k)) for k in KEY_NAMES)
    per_key = int(CONFIG.get("credits_per_key_per_month", 10000))
    if not runs or not credits_this_run:
        return ""
    monthly = credits_this_run * runs * 30
    allowance = keys * per_key
    needed = -(-monthly // per_key)
    line = (f"\U0001F4CA Credits: ~{credits_this_run} this run \u00d7 {runs} "
            f"runs/day \u2248 {monthly:,}/month. Your {keys} key"
            f"{'s' if keys != 1 else ''} allow ~{allowance:,}.")
    if monthly <= allowance:
        line += " \u2705"
    else:
        line += (f"\n\u26A0\uFE0F Not enough: you need about {needed} keys "
                 "at this schedule, or send /categories to scan fewer per run.")
    return line


def key_report():
    """Per-account health for the status message. Uses key numbers only."""
    lines = []
    for env_name, provider in KEY_NAMES.items():
        keys = key_list(env_name)
        if not keys:
            continue
        spent = sum(v for (pv, _), v in KEY_USE.items() if pv == provider)
        lines.append(f"\U0001F511 {provider}: {len(keys)} key"
                     f"{'s' if len(keys) > 1 else ''} \u00b7 ~{spent} credits "
                     "this run")
        for num in range(1, len(keys) + 1):
            status = KEY_STATUS.get((provider, num))
            if status is None:
                status = ("out of credits" if key_is_resting(keys[num - 1])
                          else "not needed this run")
            used = KEY_USE.get((provider, num), 0)
            icon = "\u274C" if status == "out of credits" else "\u2705"
            lines.append(f"   {icon} key {num}: {status}"
                         + (f" \u00b7 ~{used} credits" if used else ""))
        if keys and all(key_is_resting(k) for k in keys):
            lines.append("   \u26A0\uFE0F Every key is out of credits - "
                         "no pages can be fetched until one resets.")
    return "\n".join(lines) if lines else "\U0001F511 Keys detected: none"


def call_providers(url):
    for name, endpoint, make, wrapper, env_name, cost in provider_specs(url):
        if not key_list(env_name):
            continue
        r = fetch_with_keys(env_name, name, endpoint, make, cost)
        if r is None:
            continue
        if r.status_code != 200:
            TRACE.append(f"{name}: HTTP {r.status_code}")
            continue
        try:
            text = r.text
            if wrapper == "scrapfly":
                text = r.json().get("result", {}).get("content", "")
        except Exception as e:
            TRACE.append(f"{name}: {type(e).__name__}")
            continue
        if len(text) > 3000:
            yield name, text


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


def add_deal(out, name, now, was, url):
    if not (name and now and was and was > now):
        return
    if url and url.startswith("/"):
        url = "https://www.amazon.ae" + url
    if not url or not url.startswith("http"):
        url = ("https://www.amazon.ae/s?k="
               + urllib.parse.quote_plus(name[:80]))
    fp = (name.lower()[:60], round(now, 2))
    if fp in _fingerprints:
        return
    _fingerprints.add(fp)
    out.append({"name": name, "now": now, "was": was,
                "pct": round((was - now) / was * 100), "url": url})


def harvest(obj, out):
    """Walk any JSON and pull out anything shaped like a discounted product."""
    if isinstance(obj, dict):
        now, was = read_prices(obj)
        add_deal(out, pick_text(obj, NAME_KEYS), now, was,
                 pick_text(obj, URL_KEYS) or "")
        for v in obj.values():
            harvest(v, out)
    elif isinstance(obj, list):
        for v in obj:
            harvest(v, out)


PARSE_STATS = {"cards": 0, "named": 0, "priced": 0, "two_prices": 0,
               "badged": 0, "corrected": 0, "unavailable": 0,
               "from_badge": 0}

NAME_PATTERNS = [
    r'<h2[^>]*aria-label="([^"]{5,250})"',
    r'<h2[^>]*>\s*<a[^>]*>\s*<span[^>]*>([^<]{5,250})</span>',
    r'<h2[^>]*>.*?<span[^>]*>([^<]{5,250})</span>',
    r'data-cy="title-recipe"[^>]*>.*?<span[^>]*>([^<]{5,250})</span>',
    r'<a[^>]*class="[^"]*a-link-normal[^"]*"[^>]*>\s*<span[^>]*>([^<]{8,250})</span>',
    r'<img[^>]*alt="([^"]{8,250})"',
]


def read_name(chunk):
    for pat in NAME_PATTERNS:
        m = re.search(pat, chunk, re.DOTALL)
        if m:
            name = html.unescape(m.group(1)).strip()
            # skip decorative alt text like "Sponsored" or star ratings
            if len(name) >= 5 and not re.match(
                    r'^(sponsored|out of \d|\d+(\.\d+)? out of)', name, re.I):
                return name
    return None


UNIT_RE = re.compile(
    r'/\s*(?:\d+(?:\.\d+)?\s*)?'
    r'(?:g|gm|gr|gram|grams|kg|mg|ml|l|lt|ltr|liter|litre|oz|fl\s*oz|lb|'
    r'count|ct|piece|pieces|pcs?|item|items|unit|units|sheet|sheets|'
    r'roll|rolls|wash|washes|load|loads|tablet|capsule|serving|nappy|'
    r'diaper|wipe|wipes|bag|bags|pod|pods|100\s*g|100\s*ml)\b', re.I)

# words Amazon shows when you cannot actually buy it at that price
UNAVAILABLE_RE = re.compile(
    r'currently unavailable|temporarily out of stock|out of stock|'
    r'sold out|no featured offers|unavailable\b|'
    r'\u063a\u064a\u0631 \u0645\u062a\u0648\u0641\u0631|'
    r'\u0646\u0641\u062f\u062a \u0627\u0644\u0643\u0645\u064a\u0629', re.I)


def visible_text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))


def is_unit_price(fragment):
    """True if this price element states a rate per gram, piece, wash, etc."""
    text = visible_text(fragment)
    parts = text.split("AED")
    own = "AED" + parts[1] if len(parts) > 1 else text
    return bool(UNIT_RE.search(own[:60]))


def is_unavailable(chunk):
    """True if the listing is out of stock, where the shown price is stale."""
    return bool(UNAVAILABLE_RE.search(visible_text(chunk)))


def _price_from(fragment):
    """Read one price out of a small piece of Amazon price markup."""
    m = re.search(r'<span class="a-offscreen">\s*(?:AED)?\s*'
                  r'([\d,]+\.?\d{0,2})', fragment)
    if m:
        return clean_number(m.group(1))
    m = re.search(r'a-price-whole"[^>]*>\s*([\d,]+)'
                  r'(?:.*?a-price-fraction"[^>]*>\s*(\d{1,2}))?',
                  fragment, re.DOTALL)
    if m:
        return clean_number(f"{m.group(1)}.{m.group(2) or '0'}")
    m = re.search(r'AED\s*([\d,]+\.?\d{0,2})', fragment)
    if m:
        return clean_number(m.group(1))
    return None


def read_card_prices(chunk):
    """Return (price_now, price_was).

    Amazon shows several prices per product - the price you pay, the
    crossed-out list price, a per-unit price, other sellers' prices. We tell
    them apart by their markup instead of guessing from the numbers, because
    guessing pairs unrelated prices and invents discounts that don't exist.
    """
    now = was = None
    for m in re.finditer(r'class="(a-price[^"]*)"([^>]*)>', chunk):
        classes, attrs = m.group(1), m.group(2)
        fragment = chunk[m.end():m.end() + 400]
        # a per-unit price like "(AED 12.50/100 g)" is not the item price
        if is_unit_price(fragment):
            continue
        value = _price_from(fragment)
        if not value:
            continue
        # only a genuinely crossed-out price is the "was" price. Amazon
        # gives unit prices (AED 12.50/100 g) the same a-text-price class,
        # but never a strike marker - that is what used to fake discounts
        # on per-gram and multi-pack listings.
        struck = ("data-a-strike" in attrs or "a-text-strike" in classes)
        if struck:
            if was is None:
                was = value
        elif now is None:
            now = value
        if now and was:
            break

    # some layouts mark the old price only with a strike tag
    if was is None:
        m = re.search(r'<(?:s|del)\b[^>]*>(.{0,200}?)</(?:s|del)>',
                      chunk, re.DOTALL)
        if m:
            was = _price_from(m.group(1))

    return now, was


# Amazon prints its own discount badge, e.g. "-45%". Trusting that is safer
# than working the percentage out from two prices we might have mispaired.
# Every pattern needs a minus sign or the word off/save, so product titles
# like "100% cotton" or "70% cocoa" can never be mistaken for a discount.
PCT_PATTERNS = [
    r'>\s*-\s*(\d{1,2})\s*%',
    r'-\s*(\d{1,2})\s*%\s*<',
    r'(\d{1,2})\s*%\s*off\b',
    r'\bsave\s*(\d{1,2})\s*%',
    r'"savings?_?[Pp]ercentage"\s*:\s*"?(\d{1,2})',
]


def read_badge_percent(chunk):
    for pat in PCT_PATTERNS:
        m = re.search(pat, chunk, re.I)
        if m:
            v = int(m.group(1))
            if 1 <= v <= 99:
                return v
    return None


def parse_amazon_html(html_text):
    """Amazon keeps prices in HTML, not JSON, so read the result cards."""
    out = []
    for block in html_text.split('data-asin="')[1:]:
        PARSE_STATS["cards"] += 1
        asin = block[:15].split('"')[0]
        chunk = block[:12000]
        name = read_name(chunk)
        if not name:
            continue
        PARSE_STATS["named"] += 1
        if is_unavailable(chunk):
            PARSE_STATS["unavailable"] += 1
            continue
        now, was = read_card_prices(chunk)
        if now or was:
            PARSE_STATS["priced"] += 1
        if now and not was:
            b = read_badge_percent(chunk)
            if b:
                was = round(now / (1 - b / 100), 2)
                PARSE_STATS["from_badge"] += 1
        if not (now and was):
            continue
        PARSE_STATS["two_prices"] += 1
        # a "was" price more than 20x the sale price is not a real discount
        if was > now * 20:
            continue
        badge = read_badge_percent(chunk)
        computed = round((was - now) / was * 100)
        if badge:
            PARSE_STATS["badged"] += 1
            if abs(badge - computed) > 2:
                # Amazon's own number wins; rebuild the old price to match so
                # the message agrees with the page
                PARSE_STATS["corrected"] += 1
                was = round(now / (1 - badge / 100), 2)
        url = f"https://www.amazon.ae/dp/{asin}" if asin else ""
        add_deal(out, name, now, was, url)
    return out


# ---------------------------------------------------------------- hunting

# ---- smarter scanning ------------------------------------------------------
# Amazon has an unofficial search filter, &pct-off=30-85, that returns only
# discounted items. Where it's honoured, every slot on the page is a deal
# instead of a handful. Where it's ignored, results are the same as without
# it - so it can only help. The run report measures which is happening.
RICH_PAGE = 10          # a page with this many deals probably has more on the next
EXTRA_PAGES = [int(CONFIG.get("extra_pages_per_run", 20))]
MAX_PAGES = int(CONFIG.get("max_pages_per_search", 3))
FILTER_STATS = {"pages": 0, "cards": 0, "discounted": 0, "no_results": 0}
SCAN_STATS = {"extra_pages": 0}

NO_RESULTS_RE = re.compile(
    r"No results for|did not match any products|"
    r"\u0644\u0627 \u062a\u0648\u062c\u062f \u0646\u062a\u0627\u0626\u062c", re.I)


def filter_on():
    v = STATE["settings"].get("use_discount_filter")
    if v is None:
        v = CONFIG.get("use_discount_filter", True)
    return bool(v)


def search_url(query, page=1, filtered=True):
    url = "https://www.amazon.ae/s?k=" + urllib.parse.quote_plus(query)
    if filtered:
        lo = int(setting("min_discount_percent"))
        hi = int(setting("max_discount_percent"))
        url += f"&pct-off={lo}-{hi}"
    if page > 1:
        url += f"&page={page}"
    return url


def fetch_page(url):
    """Fetch one results page. Returns (deals, cards, source, no_results).

    Stops at the first provider that returns a REAL results page - even one
    with no discounts. Escalating a perfectly good page to the 10-credit
    browser mode was pure waste; escalation is now only for broken pages."""
    before = PARSE_STATS["cards"]
    for name, html_text in call_providers(url):
        deals = parse_amazon_html(html_text)
        cards = PARSE_STATS["cards"] - before
        no_results = bool(NO_RESULTS_RE.search(html_text[:300000]))
        if cards or no_results:
            return deals, cards, name, no_results
    return [], PARSE_STATS["cards"] - before, None, False


def scan_search(query):
    """One keyword, going deeper only while pages stay rich in deals."""
    filtered = filter_on()
    deals, any_cards, any_empty = [], False, False
    page = 1
    while True:
        page_deals, cards, src, no_results = fetch_page(
            search_url(query, page, filtered))
        if filtered:
            FILTER_STATS["pages"] += 1
            FILTER_STATS["cards"] += cards
            FILTER_STATS["discounted"] += len(page_deals)
            FILTER_STATS["no_results"] += int(no_results)
        if src and page_deals:
            SOURCES[src] = SOURCES.get(src, 0) + len(page_deals)
        deals += page_deals
        any_cards = any_cards or cards > 0
        any_empty = any_empty or no_results
        if (len(page_deals) >= RICH_PAGE and page < MAX_PAGES
                and EXTRA_PAGES[0] > 0):
            EXTRA_PAGES[0] -= 1
            SCAN_STATS["extra_pages"] += 1
            page += 1
            time.sleep(1)
            continue
        return deals, any_cards, any_empty


def check_amazon(queries):
    found = []
    sapi = key_list("SCRAPERAPI_KEY")
    for query in queries:
        got = []
        any_cards = any_empty = False
        # 1. ScraperAPI's ready-made Amazon reader (only if you use ScraperAPI)
        if sapi:
            r = fetch_with_keys(
                "SCRAPERAPI_KEY", "scraperapi structured",
                "https://api.scraperapi.com/structured/amazon/search",
                lambda k, q=query: {"api_key": k, "query": q,
                                    "tld": "ae", "country": "ae"},
                1, timeout=120)
            if r is not None and r.status_code == 200:
                try:
                    harvest(r.json(), got)
                except Exception as e:
                    TRACE.append(f"structured: {type(e).__name__}")
                if got:
                    SOURCES["scraperapi structured"] = SOURCES.get(
                        "scraperapi structured", 0) + len(got)
        # 2. the search results pages themselves
        if not got:
            got, any_cards, any_empty = scan_search(query)
        if not got:
            if any_cards:
                label = "page ok, no discounts"
            elif any_empty:
                label = "no items at this discount"
            else:
                label = "page not usable"
            SOURCES[label] = SOURCES.get(label, 0) + 1
        found += got
        time.sleep(1)
    return found


def rotate(terms, per_run, cursor):
    """This run's slice of the keyword list, plus where to resume."""
    terms = [t for t in terms if t and t.strip()]
    if not terms or per_run <= 0:
        return [], cursor
    if per_run >= len(terms):
        return terms, 0
    start = cursor % len(terms)
    doubled = terms + terms
    return doubled[start:start + per_run], (start + per_run) % len(terms)


# ---------------------------------------------------------------- main

def main():
    read_commands()

    if COMMANDS_ONLY:
        save_state()
        log("Commands processed.")
        return

    if STATE["paused"]:
        save_state()
        if IS_MANUAL_RUN:
            send_telegram("\u23F8 Hunting is paused. Send /resume to start "
                          "again.", to=CHAT_ID)
        log("Paused - no hunting this run.")
        return

    min_pct = setting("min_discount_percent")
    max_pct = setting("max_discount_percent")
    terms = CONFIG.get("amazon_searches", [])
    queries, next_cursor = rotate(terms, int(setting("searches_per_run")),
                                  int(STATE["cursor"]))
    log(f"Scanning {len(queries)} of {len(terms)} categories")

    if not any_key_set():
        send_telegram(
            "\u26A0\uFE0F No scraping key reached me. Amazon blocks plain "
            "requests, so I cannot read prices.\n\nI support these secret "
            "names:\n"
            + "\n".join(f"\u2022 <code>{k}</code>" for k in KEY_NAMES)
            + "\n\nTwo things to check:\n1. The secret's NAME is spelled "
              "exactly as above.\n2. The same name appears in the <b>env:</b> "
              "block of <code>.github/workflows/dealbot.yml</code> - a secret "
              "GitHub doesn't pass through never reaches me.", to=CHAT_ID)
        save_state()
        return

    deals = check_amazon(queries)
    log(f"Discounted products found: {len(deals)}")

    hot = [d for d in deals if min_pct <= d["pct"] <= max_pct]
    skipped = len([d for d in deals if d["pct"] > max_pct])
    if skipped:
        TRACE.append(f"skipped {skipped} results above {max_pct:.0f}% "
                     "(usually size/colour price ranges, not real deals)")

    now_ts = time.time()
    fresh = []
    for d in sorted(hot, key=lambda x: -x["pct"]):
        key = f"{d['name'][:60].lower()}|{int(d['now'])}"
        if now_ts - STATE["seen"].get(key, 0) < REMEMBER_DAYS * 86400:
            continue
        fresh.append((key, d))

    alerts = fresh[:int(setting("max_alerts_per_run"))]
    log(f"New deals >= {min_pct:.0f}%: {len(fresh)} (sending {len(alerts)})")

    if alerts:
        lines = []
        for key, d in alerts:
            STATE["seen"][key] = now_ts
            lines.append(
                f"\U0001F525 <b>\u2212{d['pct']}%</b>\n"
                f"{html.escape(d['name'][:90])}\n"
                f"<b>AED {d['now']:,.0f}</b>  <s>AED {d['was']:,.0f}</s>"
                f"  (save AED {d['was'] - d['now']:,.0f})\n"
                f"<a href=\"{html.escape(d['url'], quote=True)}\">"
                f"Open on Amazon \u2197</a>")
        chunk = (f"\U0001F985 <b>Deal Falcon</b> \u2014 {len(alerts)} deal"
                 f"{'s' if len(alerts) > 1 else ''} \u2265 {min_pct:.0f}% off"
                 "\n\n")
        for line in lines:
            if len(chunk) + len(line) > 3500:
                send_telegram(chunk)
                chunk = ""
            chunk += line + "\n\n"
        if chunk.strip():
            send_telegram(chunk)

    STATE["cursor"] = next_cursor
    STATE["last_run"] = {"time": int(now_ts), "credits": sum(KEY_USE.values()),
                         "deals": len(deals), "new": len(fresh)}
    for k in [k for k, ts in STATE["seen"].items()
              if now_ts - ts > REMEMBER_DAYS * 86400]:
        del STATE["seen"][k]
    save_state()

    if IS_MANUAL_RUN:
        status = (f"\u2705 Deal Falcon is alive.\n"
                  f"Scanned {len(queries)}/{len(terms)} categories \u00b7 "
                  f"found {len(deals)} discounted products \u00b7 "
                  f"{len(fresh)} passed your \u2265{min_pct:.0f}% rule.")
        if SOURCES:
            status += "\n\n\U0001F4E1 Data came from:\n" + "\n".join(
                f"\u2022 {html.escape(k)}: {v}"
                for k, v in sorted(SOURCES.items(), key=lambda x: -x[1]))
        ps = PARSE_STATS
        if ps["cards"]:
            status += (f"\n\n\U0001F9EE Parser saw: {ps['cards']} product "
                       f"cards, {ps['named']} readable, {ps['priced']} "
                       f"with a price, {ps['two_prices']} with a was-price, "
                       f"{ps['badged']} showing Amazon's own % badge "
                       f"({ps['corrected']} corrected to match it). "
                       f"Skipped {ps['unavailable']} out-of-stock listings.")
        if FILTER_STATS["pages"]:
            fs = FILTER_STATS
            share = (100 * fs["discounted"] // fs["cards"]) if fs["cards"] else 0
            verdict = ("working \u2705" if share >= 40 else
                       "probably ignored by Amazon.ae - harmless, same "
                       "results as without it" if fs["cards"] else
                       "no data yet")
            status += (f"\n\n\U0001F3AF Discount filter: {verdict}\n"
                       f"   {fs['discounted']} of {fs['cards']} products on "
                       f"filtered pages were deals ({share}%). "
                       f"{fs['no_results']} searches had nothing at this "
                       "discount.")
        if SCAN_STATS["extra_pages"]:
            status += (f"\n\U0001F4C4 Went deeper on rich searches: "
                       f"{SCAN_STATS['extra_pages']} extra pages.")
        status += "\n\n" + key_report()
        fc = credit_forecast(sum(KEY_USE.values()))
        if fc:
            status += "\n" + fc
        if TRACE:
            counts = {}
            for t in TRACE:
                counts[t] = counts.get(t, 0) + 1
            status += "\n\n\U0001F50D Notes:\n" + "\n".join(
                f"\u2022 {html.escape(k)}"
                + (f" (x{v})" if v > 1 else "")
                for k, v in sorted(counts.items(), key=lambda x: -x[1])[:8])
            if any("401" in k or "403" in k for k in counts):
                status += ("\n\n\u26A0\uFE0F A 401/403 means the key was "
                           "rejected. Check the secret's NAME matches your "
                           "provider, and that the key is still active.")
        status += "\n\nSend /help to change settings from here."
        send_telegram(status, to=CHAT_ID)

    for t in TRACE:
        log("TRACE: " + t)


if __name__ == "__main__":
    main()
