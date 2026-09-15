# Discord Keyword Responder Bot

A Python Discord bot with keyword auto-responses, mood-based teases, reminders, per-server daily jokes, sponsorship tags, a **wishlist** price tracker (scrape loop, DMs on price/stock changes, buy/wait signals, and history graphs), and a per-user **flight price tracker**. A separate **Flask API** manages the same data from scripts or other tools. Both processes share one SQLite database and are typically kept alive with **PM2**.

---

## Tech stack

| Piece | Role |
|---|---|
| Python 3.x | Runtime |
| [discord.py](https://github.com/Rapptz/discord.py) | Bot |
| SQLite3 | Persistence (`responses.db` by default) |
| [Flask](https://flask.palletsprojects.com/) | REST API (`api.py`) |
| [PM2](https://pm2.keymetrics.io/) | Process manager |
| [pytest](https://pytest.org/) | Tests (`requirements-dev.txt`) |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | Local LLM inference through `llama-server` |
| `curl_cffi` (optional) | TLS fingerprinting for bot-protected shops; falls back to `requests` |

**Why two Python entry points?** `scraper.py` holds pure HTTP/HTML parsing with no Discord or Matplotlib imports. `api.py` imports only `scraper.py`, so the API process stays light. `features/scraping.py` adds Discord commands, graphs, currency conversion, and alerts on top of the same scraper.

---

## Getting started

### Prerequisites

- Python 3.x and pip
- SQLite3 (usually bundled with Python)
- Node.js + npm (for PM2)
- `llama-server` with a compatible GGUF instruct/chat model
- For graphs: Matplotlib (in `requirements.txt`)

### Install dependencies

```bash
npm install pm2 -g && pm2 update
pip install -r requirements.txt
```

With a venv, use `./venv/bin/pip` instead of `pip`.

**Low-resource ARM devices (e.g. Raspberry Pi Zero W)** — prefer system packages to avoid long compiles:

```bash
sudo apt update
sudo apt install python3-flask python3-requests python3-bs4 python3-matplotlib \
  python3-psutil python3-dotenv python3-cffi python3-certifi
pip install --break-system-packages discord.py curl_cffi
```

On Pi Zero W, `curl_cffi` may fail to build; the scraper still works via plain `requests`, but sites with anti-bot TLS checks (e.g. some Romanian retailers) may not scrape.

Check optional TLS impersonation:

```bash
python3 -c "from curl_cffi import requests; print('OK')"
```

### Configuration

Create `.env` in the project root:

```env
DISCORD_TOKEN=YOUR_DISCORD_TOKEN_HERE
HOST=YOUR_HOST_HERE
PORT=YOUR_PORT_HERE
API_TOKEN=YOUR_API_TOKEN_HERE
LLAMA_CPP_BASE_URL=http://127.0.0.1:8080
LLAMA_CPP_DEFAULT_MODEL=discord-bot
LLAMA_CPP_ALLOWED_MODELS=discord-bot
```

| Variable | Required | Notes |
|---|---|---|
| `DISCORD_TOKEN` | Yes (bot) | Bot refuses to start without it. |
| `BOT_ID` | Yes (bot) | Your bot's Discord user ID (Developer Mode → right-click bot → Copy User ID). Used to recognize @mentions addressed to the bot. |
| `HOST` | Yes (API) | Bind address. Use `0.0.0.0` for LAN/Tailscale or **Docker** (published ports). Use `127.0.0.1` only if the API should be local to the host (e.g. PM2, no remote access). |
| `PORT` | Yes (API) | e.g. `9999`. |
| `API_TOKEN` | Strongly recommended | Every API route expects `Authorization: Bearer <token>`. If unset, the API runs **unauthenticated** and logs a CRITICAL warning. |
| `DB_FILE` | No | Full path to the SQLite file (filename included), e.g. `/var/lib/discord-bot/responses.db`. Default: `responses.db` in the working directory. Parent dirs are created automatically. |
| `LLAMA_CPP_BASE_URL` | No (bot) | `llama-server` base URL. Default: `http://127.0.0.1:8080`. Docker defaults to `http://host.docker.internal:8080`. A URL ending in `/v1` is also accepted. |
| `LLAMA_CPP_DEFAULT_MODEL` | Yes (bot) | Default model alias passed to `llama-server`. Must be listed in `LLAMA_CPP_ALLOWED_MODELS`. Match the alias supplied to `llama-server --alias`. |
| `MENTION_LLAMA_CPP_MODEL` | No (bot) | Model alias for @bot mentions. Defaults to `LLAMA_CPP_DEFAULT_MODEL` and must be allowed. |
| `LLAMA_CPP_ALLOWED_MODELS` | Yes (bot) | Comma-separated llama.cpp model aliases offered by `/llm_set`. A single-server setup normally lists one alias. |
| `LLAMA_CPP_TIMEOUT` | No | Internal HTTP limit for llama.cpp generation calls. Default: `180`. |
| `LLAMA_CPP_API_KEY` | No | Optional bearer token when `llama-server` is configured to require an API key. |
| `ASK_COOLDOWN_SECONDS` | No (bot) | Per-user cooldown for mentions after each answer finishes. Default: `60` (1 minute). |
| `LLM_CONTEXT_MESSAGES` | No (bot) | Number of recent channel messages to include as context for mentions. Default: `0`. |
| `TEASE_LLM_ENHANCE` | No (bot) | Rewrite random teases through llama.cpp. Default: `true`. Set `false` to disable generated teases. |
| `TEASE_LLAMA_CPP_MODEL` | No (bot) | Model alias for tease rewrites. Defaults to `LLAMA_CPP_DEFAULT_MODEL`. |
| `TEASE_LLAMA_CPP_TIMEOUT` | No (bot) | Seconds to wait for a tease rewrite. Default: `45`. |
| `SERPAPI_BASE_URL` | No | Google Flights search endpoint. Default: `https://serpapi.com/search.json`. |
| `SERPAPI_ACCOUNT_URL` | No | API-key validation endpoint. Default: `https://serpapi.com/account.json`. |
| `SERPAPI_TIMEOUT` | No | SerpApi HTTP timeout in seconds. Default: `30`. |
| `SERPAPI_GL` | No | Google Flights country context. Default: `ro`. |
| `SERPAPI_HL` | No | Google Flights response language. Default: `en`. |
| `FLIGHT_CHECK_INTERVAL_HOURS` | No | Hours between flight tracker passes. Default: `5`; raise this if the account approaches its monthly quota. Minimum: `1`. |
| `FLIGHT_CHECK_GAP_SECONDS` | No | Delay between users' flight trackers in one pass. Default: `1`. |

Start llama.cpp before the bot. The alias must match
`LLAMA_CPP_DEFAULT_MODEL`:

```bash
llama-server \
  --model /path/to/model.gguf \
  --alias discord-bot \
  --host 127.0.0.1 \
  --port 8080 \
  --ctx-size 4096
```

The bot calls llama.cpp's OpenAI-compatible `/v1/chat/completions` endpoint.
It sends only a `user` message and does not inject a `system` message; persistent
identity and behavior should be configured with the model or llama.cpp chat
template. If multiple aliases are listed, each one must be reachable through the
configured endpoint (for example through a compatible model router).

The database file and its `-wal` / `-shm` sidecars are **gitignored** — back up `responses.db` yourself (e.g. `sqlite3 .backup`), not via git.

Generate a token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Example API call (list keyword responses):

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" "http://localhost:$PORT/keywords/get?guild_id=YOUR_GUILD_ID"
```

List all wishlist items:

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" "http://localhost:$PORT/wishlist/all" | python3 -m json.tool
```

---

## Docker

One image, **two containers**: `bot` (Discord) and `api` (Flask). They share the `bot-data` volume for the database and logs.

```bash
# Create .env first (see Configuration above), then:
docker compose up -d --build
docker compose logs -f
```

| Service | Role |
|---|---|
| `bot` | Discord client (`discord-bot`) |
| `api` | Flask on `HOST`:`PORT` from `.env` — port published to the host (`discord-api`) |

API URL: `http://localhost:9999` (or your `PORT`). For Docker, set **`HOST=0.0.0.0`** in `.env` so the published port is reachable; `127.0.0.1` only listens inside the container.

Data persists in the `bot-data` volume (`/data/responses.db`, logs in `/data/logs/`). To back up:

```bash
docker compose exec bot sqlite3 /data/responses.db ".backup '/data/responses-backup.db'"
docker cp discord-bot:/data/responses-backup.db ./responses-backup.db
```

Rebuild after code changes: `docker compose up -d --build`. Stop: `docker compose down` (volume kept unless you pass `-v`).

---

## Running the bot and API (PM2)

Two separate PM2 processes:

```bash
# Bot (Discord)
pm2 start bot.py --interpreter python3 --name discord-bot

# API (Flask) — must be running for external HTTP access; bot does not start this for you
pm2 start api.py --interpreter python3 --name discord-api

pm2 save
pm2 startup   # optional: resurrect after reboot
```

With a venv, point `--interpreter` at `./venv/bin/python3`.

| Command | Purpose |
|---|---|
| `pm2 status` | See if `discord-bot` and `discord-api` are online |
| `pm2 logs` | Tail logs (`bot.log`, `api.log`, or `$LOG_DIR` if set) |
| `pm2 restart discord-bot` | Restart bot only (e.g. after code pull) |
| `pm2 restart discord-api` | Restart API only |
| `pm2 restart all` | Restart both |

After `git pull`, restart both if either `db.py` schema or slash commands changed. The bot syncs slash commands on `on_ready`.

---

## Background tasks

| Feature | Interval | What it does |
|---|---|---|
| Wishlist scrape | 12 h | Fetches each tracked URL, updates price/stock, appends history, trims entries older than 180 days, DMs on change / back-in-stock / buy-wait alerts |
| Flight tracker | 5 h (configurable) | Checks the least-recently searched fixed-date tracker for each user, stores price history, and DMs on the first result or a lower price |
| Exchange rates | 24 h | Refreshes EUR-based rates for RON, DKK, EUR, USD, GBP |
| Daily joke | 30 s check | Per subscribed guild: posts one joke in the configured window once per day |
| Reminders | 10 s | Delivers due reminders |
| Inactivity nudge | 30 min | Nudges quiet guild channels where `/llm_inactivity` is activated |
| Sponsors | 1 h | Expiry warning and cleanup |
| Teases | On message | Random mood lines rewritten via llama.cpp |

---

## Bot commands

### Keywords and chat

| Command | Description |
|---|---|
| `/keyword_add <keyword> <response>` | Add a keyword → response pair **for this server only** (random pick when multiple). |
| `/topkeywords [user]` | Most triggered keywords in the server. |
| `/mood <mood>` | Set tease mood; random teases are rewritten via llama.cpp in that style. |
| `/help` | Full command list (chunked for Discord’s 2000-character limit). |

### Reminders

| Command | Description |
|---|---|
| `/remind <when> <who> <what>` | Timed reminder — `when` like `30m`, `2h`, `1d`. |

### Daily jokes (per server)

The joke **pool** is global; **schedule and “already sent” history** are per guild.

| Command | Description |
|---|---|
| `/joke_add <text>` | Add text to the shared pool. |
| `/joke_activation <time>` | Enable daily joke in **this channel** at `HH:MM` (e.g. `14:00`). Each server configures independently. |
| `/joke_deactivation` | Disable for this server (sent history kept). |
| `/joke_status` | Ephemeral: channel, time, last sent date, or not activated. |

On first boot after upgrading from single-guild jokes, the bot migrates the old global channel/time settings into one `guild_joke_config` row automatically.

### Sponsors

| Command | Description |
|---|---|
| `/sponsor_set [user] [plan]` | Password modal; optional plan tier and custom message (top tier). |
| `/sponsor_plans` | Plans, prices, append chance on keyword replies. |
| `/sponsor_who` | Current sponsor and time until 1-year expiry. |

### Wishlist (price tracking)

| Command | Description |
|---|---|
| `/wishlist-item <url>` | Track URL; live scrape on add. Checked every 12 h; DMs on price/stock changes. |
| `/wishlist-item-delete <url>` | Remove item and its price history. |
| `/wishlist-target-price <url> <price> <currency>` | Notify once when an item reaches the configured price or lower; target currency may differ from the shop currency. |
| `/wishlist-target-clear <url>` | Remove one target-price alert. |
| `/wishlist-restock-only <url> <enabled>` | Suppress price/target DMs for this item while continuing to track it; only back-in-stock changes notify. |
| `/wishlist-refresh <url>` | Fetch one item immediately and report its source, freshness, stock, price, and target progress. Five-minute per-item cooldown. |
| `/wishlist-show [currency]` | List your items. Default: each item’s native currency. Optional: `RON`, `DKK`, `EUR`, `USD`, `GBP`. |
| `/wishlist-graph <url> [currency]` | PNG price history for one URL (up to 180 days). |
| `/wishlist-graph-all [currency]` | Combined graph for all your items; default currency = majority across your list. |

**Currency** — read from the page when possible; TLD fallback (e.g. `.ro` → RON, `.dk` → DKK).

**Text-fallback extraction** — If JSON-LD or meta tags are missing, the scraper strips `<script>` and `<style>` tags to check visible page text for stock status keywords, preventing false "out of stock" readings triggered by hidden JS localization strings.

**AI Fallback Scraping** — If standard HTML metadata is missing, the scraper strips the page text and uses local llama.cpp (`LLAMA_CPP_DEFAULT_MODEL`) to extract price and stock data from unstructured web text.

**Price-change DMs** include exact old/new prices plus a short llama.cpp-generated reaction. The model randomly varies between funny, mock-corporate, playful, serious, enthusiastic, and melodramatically sad tones, and is told whether the observed price increased or decreased. If generation fails, the factual notification is still delivered.

**Buy / wait DMs** (after ~7 data points and ≥1 % price spread in the window):

- Green — at or below rolling all-time low (“buy window”); re-alerts only on a further ≥1 % drop.
- Red — above historical median (“maybe wait”); one alert per high period until price returns to median or below.

Flat prices do not trigger spurious “all-time low” messages.

**Target alerts and source status** — a target fires once when its price crosses
at or below the chosen amount, then re-arms only after rising above it. The item
list reports the source domain and the outcome/time of its latest check.

### Flight tracker (per user)

| Command | Description |
|---|---|
| `/flight_tracker_add <origin> <destination> <start_date> <end_date> [adults] [currency]` | Save a fixed-date round-trip watch and run its first search immediately. On first use, opens the private SerpApi login modal. Use three-letter IATA city/airport codes and `YYYY-MM-DD`. |
| `/flight_tracker_show` | List only your trackers, IDs, periods, latest best dates/prices, and any provider error. |
| `/flight_tracker_delete <tracker_id>` | Delete only your own tracker and its price history. |
| `/flight_tracker_login` | Validate and set/replace your own SerpApi API Key. |
| `/flight_tracker_logout` | Remove your saved SerpApi key. Existing trackers remain saved but checks pause until the next login. |

Exact example: `/flight_tracker_add origin:OTP destination:BKK start_date:2026-12-30 end_date:2027-01-13`.

Only fixed departure and return dates are supported. Flexible date windows were intentionally removed because each date pair would consume a separate SerpApi search and quickly exhaust the free quota.

Each Discord user supplies one **SerpApi API Key**. If no login exists when `/flight_tracker_add` is used, the bot opens a modal, validates the key through SerpApi's Account API, saves it, and then creates the tracker. Later adds reuse that same user's key and quota. Keys are stored in the local SQLite database and are never shown by commands or written to logs. Because SQLite storage is not encrypted, filesystem/database access must be restricted to the bot operator; use `/flight_tracker_logout` to remove a user's key.

The free SerpApi plan currently includes 250 searches per month. To stay below that, each five-hour scheduled pass checks at most one tracker per user/API key, selecting the least-recently checked one. This caps scheduled usage at about 144 successful searches per user in a 30-day month regardless of how many trackers are saved; multiple trackers rotate and are therefore checked less often individually. Immediate searches performed by `/flight_tracker_add` use additional credits from the remaining headroom. See the [SerpApi Google Flights documentation](https://serpapi.com/google-flights-api) and [pricing](https://serpapi.com/pricing).

### System

| Command | Description |
|---|---|
| `/stats` | Portable Windows/Linux/macOS host stats: platform, CPU/cores, RAM, current drive/filesystem, network, uptime, and bot memory. Temperature/load show `N/A` when the host does not expose them. |
| `/llm_set <model>` | Set the allowed llama.cpp model alias used when the bot is mentioned. **60s cooldown** per user for mentions. |
| `/llm_inactivity <activate\|deactivate>` | Enable or disable LLM-generated inactivity nudges for this server. Requires **Manage Server** permission. Existing servers default to enabled. |
| `@bot` | Silent reply in-thread — no model/Q/thinking UI. Empty ping → short prompt back; with text → one direct LLM answer. |
| `@bot <text>` | Uses `MENTION_LLAMA_CPP_MODEL` and the configured recent context to resolve brief questions; returns one ready-to-send reply in the current message's language rather than response options. |
| `/llm_feedback_summary` | Manage Server only; compare this server’s rated reply configurations. |

LLM mention replies include 👍 and 👎 reactions. Only the user who made the
request can rate the answer; the database retains only reply metadata, server,
model alias, prompt version, final rating, and timestamps—not prompts or
response text. `/llm_feedback_summary` marks a model/prompt combination
ready to compare only after ten ratings; it never changes a model or prompt
automatically.

---

## REST API

All routes require `Authorization: Bearer <API_TOKEN>` when `API_TOKEN` is set. Base URL: `http://<HOST>:<PORT>` (from `.env`).

### Keywords

| Method | Path | Body / notes |
|---|---|---|
| `POST` | `/keywords/add` | `{ "guild_id", "keyword", "response" }` |
| `DELETE` | `/keywords/delete` | `{ "guild_id", "keyword", "response"? }` — omit `response` to delete all for keyword in that guild |
| `GET` | `/keywords/get?guild_id=<id>` | Map of keyword → list of responses for one server |

### Reminders

| Method | Path | Body / notes |
|---|---|---|
| `POST` | `/reminders/add` | `{ "user_id", "channel_id", "remind_at", "message" }` — `remind_at` Unix timestamp |
| `DELETE` | `/reminders/delete/<id>` | |
| `GET` | `/reminders/all` | Array of reminder objects |

### Jokes (pool)

| Method | Path | Body / notes |
|---|---|---|
| `GET` | `/jokes` | All jokes `{ id, text, sent }` (`sent` is legacy column; per-guild tracking uses `guild_joke_sent`) |
| `GET` | `/jokes/<id>` | One joke |
| `POST` | `/jokes` | `{ "text" }` |
| `PUT` | `/jokes/<id>` | `{ "text" }` |
| `DELETE` | `/jokes/<id>` | |
| `POST` | `/jokes/reset` | Clears **per-guild** sent history for all guilds (pool unchanged) |

### Jokes (per-guild schedule)

| Method | Path | Body / notes |
|---|---|---|
| `GET` | `/jokes/guilds` | All guild configs |
| `GET` | `/jokes/guilds/<guild_id>` | One config or 404 |
| `PUT` | `/jokes/guilds/<guild_id>` | `{ "channel_id", "send_time": "HH:MM" }` — create or update |
| `DELETE` | `/jokes/guilds/<guild_id>` | Deactivate guild |

### Wishlist

| Method | Path | Body / notes |
|---|---|---|
| `POST` | `/wishlist/add` | `{ "user_id", "url" }` — **live scrape**; `201` with item fields, or `400` / `409` / `422` / `502` |
| `DELETE` | `/wishlist/remove` | `{ "user_id", "url" }` |
| `GET` | `/wishlist/all` | All tracked items incl. `last_alert_kind`, `last_alert_price` |

`POST /wishlist/add` may take up to ~15 s (HTTP timeout). It does not create rows for blocked or unsupported pages.

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

**CI** — GitHub Actions runs `pytest` on every push/PR (`.github/workflows/test.yml`) and builds the Docker image plus validates `docker-compose.yml` (`.github/workflows/docker.yml`).

Coverage highlights: `db.py` (CRUD, stock tri-state, FK cascades, flight tracker user isolation, exchange rates, **per-guild joke** config/sent isolation), `flight_provider.py` (SerpApi key validation and Google Flights response parsing), registered flight login/add/show/delete command callbacks, `scraper.py` (JSON-LD, meta tags, TLD currency, URL validation), `features/scraping` currency and **alert classifier**, and `features/keywords` response picker.

Tests use an isolated DB per case (`tests/conftest.py`); your live `responses.db` is never touched.

---

## Project layout

### Root

| File | Role |
|---|---|
| `bot.py` | Discord client, feature wiring, `on_message` / `on_ready` |
| `api.py` | Flask API (lazy `init_db` on first request) |
| `scraper.py` | `PriceScraper`, `ScrapeResult`, parsing helpers — **no** discord/matplotlib |
| `flight_provider.py` | SerpApi Account/Google Flights client and IATA/date validation — **no** Discord imports |
| `db.py` | SQLite schema and queries |
| `logger.py` | Rotating logs (5 MB × 2); optional `LOG_DIR` env for log file location |
| `Dockerfile`, `docker-compose.yml` | Docker image and bot + API services |
| `responses.db` | Runtime DB (gitignored); path overridable via `DB_FILE` |

### `features/`

| Module | Class | Role |
|---|---|---|
| `response_gate.py` | `ResponseGate` | Cooldown between keyword replies and teases |
| `keywords.py` | `KeywordsFeature` | Per-guild keyword match, `/keyword_add`, `/topkeywords` |
| `teases.py` | `TeasesFeature` | Mood teases (LLM-enhanced), `/mood` |
| `tease_llm.py` | — | LLM prompts and generation helpers for teases and mentions |
| `llm_client.py` | — | Shared llama.cpp `/v1/chat/completions` client |
| `inactivity.py` | `InactivityFeature` | Guild activity tracking, inactivity nudges |
| `reminders.py` | `RemindersFeature` | `/remind`, delivery loop |
| `jokes.py` | `JokesFeature` | Joke pool + per-guild schedule commands and loop |
| `sponsors.py` | `SponsorsFeature` | Sponsor tiers, modal, expiry |
| `scraping.py` | `ScrapingFeature`, `CurrencyConverter` | `/wishlist-*`, scrape loop, graphs, alerts (imports `PriceScraper` from `scraper.py`) |
| `flights.py` | `FlightTrackerFeature` | `/flight_tracker_*` login and tracker commands, immediate searches, five-hour checks, lower-price DMs |
| `stats.py` | `StatsFeature` | `/stats` |
| `llm_mention.py` | `LLMMentionFeature` | Queued @bot mention prompts through llama.cpp |
| `llm_feedback.py` | `LLMFeedbackFeature` | Requester-only 👍/👎 ratings for generated mention replies |
| `mention_utils.py` | — | Parse @bot mentions using `BOT_ID` |
| `help_feature.py` | `HelpFeature` | `/help` |
