# Forum Thread Finder — a generic XenForo search Telegram bot for Railway

A generic, site-agnostic **XenForo forum search agent**. Send a term (a
person's name / topic) → the bot logs into the configured XenForo-based
board, runs the board's keyword search, crawls the matching thread(s)
page by page, and replies with a structured report of all outbound
**content-hosting links** (mega, bunkr, cyberdrop, pixeldrain, ...) and
**social/profile links** (OnlyFans, Twitter/X, Instagram, Reddit, ...).

Works with any standard XenForo board. The target site is entirely
configured through environment variables — nothing is hardcoded.

Login and crawl logic follows the same well-documented techniques used
by the open-source [lols](https://github.com/vladimirpolak/lols)
scraper (XenForo CSRF login flow, `rel="next"` pagination), wrapped in
a friendly Telegram interface.

## How it looks

```
you:      /find jane doe
bot:      🔎 Forum results for "jane doe"
          🧵 2 thread(s) · 📄 47 pages scanned
          📁 12 content · 🌐 3 social · 🖼 8 image links

          ━━ Jane Doe discussion thread (23 pages)
          📁 Content links (7):
          • Mega
          • Bunkr
          ...
```

Big reports are automatically sent as a downloadable `.txt` file.

## Setup

### 1. Create the Telegram bot

1. Open Telegram, talk to **@BotFather** → `/newbot` → follow the prompts.
2. Copy the **bot token** (`123456:ABC-...`).

### 2. Push this folder to GitHub

```
forum-finder/
├── bot.py            # the whole app
├── requirements.txt
├── Procfile
├── railway.json      # optional health check config
└── README.md
```

1. Create a new repo on GitHub (keep it **private**).
2. Upload these files (GitHub web: *Add file → Upload files → drag the 5 files*). No secrets are in the code — everything is a Railway env var.

### 3. Deploy on Railway

1. [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo** → pick the repo.
2. Service → **Variables** → add:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from BotFather |
| `FORUM_BASE_URL` | base URL of your XenForo board, e.g. `https://board.example` |
| `FORUM_USERNAME` | your forum account username |
| `FORUM_PASSWORD` | your forum account password |
| `ALLOWED_CHAT_IDS` | *(recommended)* chat ids allowed to use the bot, comma-separated. Send `/id` to the bot to get one. |

3. **Settings**:
   - Start command is auto-detected from `Procfile` (`python bot.py`). Nothing to change.
   - Railway will auto-detect **Python** via `requirements.txt`.
   - No volume, no database, no port needed (bot uses polling).
4. Deploy → check **Deploy logs** for `Bot starting (forum: board.example)`.

### 4. Use it

- In your private chat with the bot (or a group you add it to), send a name:
  - `/find jane doe` — or just type `jane doe`
- `/id` prints the chat id → put it in `ALLOWED_CHAT_IDS` to lock the bot down.

## What it does (per search)

1. Logs into the configured board with your account (XenForo CSRF login flow).
2. Runs the board's keyword search, relevance order.
3. Takes up to `MAX_THREADS_TO_CRAWL` (default **3**) best matches — exact title matches first.
4. Crawls every page of each thread (follows `rel="next"`, max `CRAWL_PAGE_LIMIT` = 50 pages, 1.2 s delay between requests).
5. Extracts and classifies links:
   - **Content**: mega, bunkr, cyberdrop, gofile, pixeldrain, erome, lovefap, streamtape/voe/etc.
   - **Social**: OnlyFans, Fansly, Twitter/X, Instagram, Reddit, Patreon, TikTok, Telegram, Discord
   - **Image hosts**: imgbox, pixhost, imgbb, ...
6. Sends the report as HTML messages, or as a `.txt` attachment when it's big.

## Tuning

Top of `bot.py`:

| Constant | Default | Meaning |
|---|---|---|
| `MAX_THREADS_TO_CRAWL` | 3 | threads fully crawled per search |
| `CRAWL_PAGE_LIMIT` | 50 | hard page cap per thread |
| `REQUEST_DELAY` | 1.2 s | delay between forum requests (be polite!) |
| `MAX_FILE_LINKS` | 150 | cap per thread in the inline reply |

## Notes / gotchas

- **No downloads** — the bot only reports links. You can paste them into `lols`/`gallery-dl` yourself to actually download.
- **Login**: if your password contains special characters, just paste it as-is into the Railway variable; no quoting needed.
- The search hits XenForo's built-in search — very short/generic terms give many false positives; exact title matches are crawled first.
- Some boards sit behind Cloudflare or have 2FA; if login fails, check the Troubleshooting table below.
- Keep the repo **private**: it's your personal tooling.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Forum login failed` in logs | Wrong `FORUM_USERNAME`/`FORUM_PASSWORD`, or wrong `FORUM_BASE_URL`. If the board has 2FA or Cloudflare bot protection, the plain-requests login may be blocked. |
| Bot never replies | Check Railway logs; token wrong → `Telegram token invalid`. |
| Search finds nothing | Term too generic or the search was rate-limited (429) — the bot retries once; try again in a minute. |
| `Search took too long` | Thread huge → lower `CRAWL_PAGE_LIMIT` or `MAX_THREADS_TO_CRAWL`. |
| Want only certain chats | Set `ALLOWED_CHAT_IDS` (comma separated). |

## Roadmap ideas

- Retry/backoff decorator around forum requests
- Simple in-memory (or Redis) cache of recent searches to avoid re-crawling
- Optionally feed found thread URLs to `lols`/`gallery-dl` for real downloads
- Support for other forum engines (phpBB, Discourse) — only the login
  mixin + pagination regex would need per-engine variants