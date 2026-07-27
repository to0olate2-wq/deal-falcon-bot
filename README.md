# Deal Falcon Bot - Amazon.ae deal hunter

Scans Amazon.ae twice a day across 140 categories, finds anything discounted
by 30% or more, and messages the deals to your phone on Telegram.
You control it entirely from Telegram - no code editing needed.

---

## Telegram commands

Message these to your bot in Telegram at any time:

| Command | What it does |
|---|---|
| `/status` | Show your current settings |
| `/discount 35` | Only alert me at 35% off or more |
| `/max 90` | Ignore discounts above 90% (usually data errors) |
| `/alerts 30` | Maximum deals in one message |
| `/categories 40` | Categories per sweep (999 = all of them) |
| `/pause` | Stop hunting |
| `/resume` | Start hunting again |
| `/forget` | Clear the memory of deals already sent |
| `/help` | Show this list |

Commands are picked up within about 30 minutes, and the bot replies to
confirm. To apply one instantly, go to the Actions tab and run
**Deal Falcon Commands** manually.

---

## The files

| File | Purpose |
|---|---|
| `deal_bot.py` | The bot. You never need to edit this. |
| `config.json` | Settings and the category list. |
| `requirements.txt` | One line: `requests` |
| `.github/workflows/dealbot.yml` | Runs the hunt twice a day |
| `.github/workflows/commands.yml` | Checks for your Telegram commands |

---

## Settings in config.json

The top of the file holds everything you'd normally change. Each setting has
a plain-English note under it. These are the same values the Telegram
commands change - whichever you set last wins.

The category list is below the settings. Add a line like `"nespresso pods"`
or delete anything you'll never buy. Fewer, sharper keywords give better
alerts and use fewer credits.

---

## Secrets you need

In your repository: **Settings -> Secrets and variables -> Actions**

| Secret | Required? |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes - from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes - from @userinfobot |
| A scraping key | Yes - any ONE of the five below |

Amazon blocks plain requests from servers, so one scraping service is
required. The bot detects whichever you set - no code changes:

`SCRAPERAPI_KEY`, `SCRAPINGBEE_KEY`, `ZENROWS_KEY`, `SCRAPEDO_KEY`,
`SCRAPFLY_KEY`

## Credits

Roughly one credit per category per sweep. With 140 categories twice a day
that's about 280 a day, or 8,400 a month. If that's too many, send
`/categories 40` in Telegram - the full list still gets covered every few
days at a quarter of the cost.

## If something breaks

Open the **Actions** tab, click the latest run, click the failing step, and
paste any red text into Claude. Websites change; the fix is usually small.
