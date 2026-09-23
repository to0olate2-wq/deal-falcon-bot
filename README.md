# Deal Falcon Bot - Amazon.ae deal hunter

Scans Amazon.ae every 6 hours across 140 categories, finds anything discounted
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
| `/filter on` / `/filter off` | Ask Amazon for discounted items only |
| `/forget` | Clear the memory of deals already sent |
| `/help` | Show this list |

Commands are picked up within about 30 minutes, and the bot replies to
confirm. To apply one instantly, go to the Actions tab and run
**Deal Falcon Commands** manually.

---

## Sharing deals with other people (private channel)

1. In Telegram: **New Channel** -> name it -> choose **Private**
2. Open the channel -> **Administrators** -> **Add Admin** -> search for your
   bot's username -> add it, leaving "Post Messages" allowed
3. The bot will message you privately with the channel's ID, and the exact
   command to run. Send that command, e.g. `/post -1001234567890`
   (if no message arrives, go to Actions -> Deal Falcon Commands ->
   Run workflow to make it check straight away)
4. Invite people to the channel using its private invite link

Deals then appear only in that channel. Your `/status`, `/discount` and error
messages still come to you privately, and channel members cannot change any
settings. Send `/private` any time to bring deals back to your own chat.

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
`SCRAPINGANT_KEY`, `SCRAPFLY_KEY`

The secret NAME must match your provider - a key sent to the wrong service
returns HTTP 401.

## How often it runs, and how many scraping accounts you need

The hunt runs every 6 hours (4 sweeps a day: 4:17, 10:17, 16:17 and 22:17 UAE time). Each sweep costs roughly one
credit per category, plus a few extra pages when a search is packed with
deals. With 140 categories:

| Schedule | Sweeps/day | Credits/month | ScrapingAnt free accounts |
|---|---|---|---|
| every 12 h | 2 | ~9,300 | 1 |
| every 6 h | 4 | ~18,600 | 2 |
| every 4 h | 6 | ~27,900 | 3 |
| every 3 h | 8 | ~37,200 | 4 |
| every 2 h | 12 | ~55,800 | 6 |
| hourly | 24 | ~111,600 | 12 |

You don't need to work this out yourself: every manual run and every
`/status` reply shows the forecast for your real schedule and key count, and
warns you if you're short.

## Credits

Roughly one credit per category per sweep. With 140 categories twice a day
that's about 280 a day, or 8,400 a month. If that's too many, send
`/categories 40` in Telegram - the full list still gets covered every few
days at a quarter of the cost.

## If something breaks

Open the **Actions** tab, click the latest run, click the failing step, and
paste any red text into Claude. Websites change; the fix is usually small.
