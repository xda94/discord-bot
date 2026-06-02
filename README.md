# 🤖 Discord Keyword Responder Bot

A lightweight, Python-powered Discord bot that monitors chat and triggers automated responses based on specific keywords. It features a Flask-based REST API for external management, configurable mood-based tease messages, reminders, daily jokes, web scraping for price tracking with dynamic currency conversion, and uses PM2 for "set-it-and-forget-it" stability.

---

## 🛠 Tech Stack

* **Language:** [Python 3.x](https://www.python.org/)
* **Library:** [discord.py](https://github.com/Rapptz/discord.py)
* **Database:** [SQLite3](https://www.sqlite.org/index.html)
* **API:** [Flask](https://flask.palletsprojects.com/)
* **Process Manager:** [PM2](https://pm2.keymetrics.io/)

---

## 🚀 Getting Started

### 1. Prerequisites
Ensure you have the following installed on your system:
* **SQLite3**
* **Python 3.x**
* **Node.js & NPM** (Required for PM2)

### 2. Installation
First, install the global process manager:
```bash
npm install pm2 -g && pm2 update
```
Now install the python dependencies:
```bash
pip3 install -r requirements.txt
```
or without venv
```bash
pip3 install --break-system-packages -r requirements.txt
```

**For Raspberry Pi Zero W (Optimized)**
To avoid extremely long compilation times on low-resource devices, it is highly recommended to install the pre-compiled system packages using `apt` instead of `pip`:
```bash
sudo apt update
sudo apt install python3-flask python3-requests python3-bs4 python3-matplotlib python3-psutil python3-dotenv python3-cffi python3-certifi
```
Then install the pip-only packages on top:
```bash
pip3 install --break-system-packages discord.py curl_cffi
```

*Why `python3-cffi` and `python3-certifi` first?* `curl_cffi` is a thin wrapper around a C extension that links against `_cffi_backend`. On ARMv6 (Pi Zero W) the prebuilt wheel is missing, so pip falls back to compiling from source — which fails unless the `cffi` and `certifi` system bindings are already present. Installing them from `apt` first lets the `pip install curl_cffi` step pick them up instead of trying (and failing) to build everything from scratch.

*If `pip install curl_cffi` still fails on your Pi*, that's fine — the scraper degrades to plain `requests` automatically. You'll lose the ability to scrape bot-protected sites (Altex, eMag, Cel.ro) but the rest of the bot works normally. You can check by running:
```bash
python3 -c "from curl_cffi import requests; print('OK')"
```
A clean `OK` means impersonation is active; an `ImportError` means you're on the fallback path.

### 3. Configuration 
Create a `.env` file in the root folder and add your credentials:
```env
DISCORD_TOKEN=your_token_here
HOST=your_host_here
PORT=your_port_here
API_TOKEN=any_long_random_string
```

`API_TOKEN` gates every Flask route via `Authorization: Bearer <token>`. Generate one with e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`. If you omit it the API still runs but is **unauthenticated** — `api.log` will record a `CRITICAL` line at startup announcing the open state. Set it before exposing the API to anything beyond `localhost`.

**Optional: relocate the database.** By default the bot stores all persistent state in `responses.db` in the working directory. Override with `DB_FILE` when you want the database to live outside the code checkout (so `git pull` / clean re-clones can never touch it):

```env
DB_FILE=/var/lib/discord-bot/responses.db
```

The value is a full path **including the filename**, not just a directory. The parent directory is auto-created on first start, and SQLite itself creates the file, so the path can point at something that doesn't exist yet. Make sure the user running `bot.py` has write permission to that directory. `responses.db` and its `-wal` / `-shm` sidecars are gitignored regardless of where they live.

Example authenticated request:
```bash
curl -H "Authorization: Bearer $API_TOKEN" http://localhost:$PORT/all
```

---

## 🏃 Execution
This project runs as two separate processes (the bot and the API). Use PM2 to keep them running in the background.

**Starting the Bot**
Without venv
```bash
pm2 start bot.py --interpreter python3 --name discord-bot
```
**Starting the API**
```bash
pm2 start api.py --interpreter python3 --name discord-api
```

With venv
**Starting the Bot**
```bash
pm2 start bot.py --interpreter ./venv/bin/python3 --name discord-bot
```
**Starting the API**
```bash
pm2 start api.py --interpreter ./venv/bin/python3 --name discord-api
```

Saving the startup
```bash
pm2 startup
```

**Useful PM2 Commands**

* `pm2 status` — Check if the bot and API are online.
* `pm2 logs` — View real-time logs and errors.
* `pm2 restart all` — Restart both processes.
* `pm2 stop all` — Stop the bot and API.

---

## 🧪 Running Tests

The repo ships with a small `pytest` suite focused on the highest-risk surfaces: `db.py` (CRUD, `COALESCE` semantics, response cache, tri-state stock, FK cascade), the pure scraping helpers (`_extract_from_json_ld`, meta extractors, TLD currency fallback, URL validation), the `CurrencyConverter`, and the `_pick_response` helper that fixed the keyword infinite-loop bug.

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Each test that touches the database uses a temporary SQLite file (via the `tmp_db` fixture in `tests/conftest.py`), so the suite never reads or writes your real `responses.db`. The bot's runtime deps (`requirements.txt`) must also be installed because the test modules import from `features/`.

---

## 🤖 Bot Commands

| Command | Description |
|---|---|
| `/keyword_add <keyword> <response>` | Add a keyword-response pair. Multiple responses per keyword are supported — the bot picks one at random. |
| `/remind <when> <who> <what>` | Set a timed reminder. Format: `30m`, `2h`, `1d`. |
| `/topkeywords [user]` | Show the most triggered keywords in the server, optionally filtered by user. |
| `/mood <mood>` | Set the bot's tease mood. Available moods are loaded dynamically from `features/teases.py`, plus `random`. |
| `/joke_add <text>` | Add a joke to the daily joke rotation. |
| `/joke_activation <time>` | Activate the daily joke in the current channel at a given time (e.g. `14:00`). |
| `/sponsor_set [user] [plan]` | Set or clear the active sponsor. Opens a password-gated modal; the optional `plan` picks one of the four tiers and unlocks a custom message on the Ultra Pro Max tier. |
| `/sponsor_plans` | List the available sponsorship plans, prices and per-tier chance of appending a sponsor tag to a keyword response. |
| `/sponsor_who` | Show the current sponsor, their plan, and time remaining until the 1-year expiry. |
| `/stats` | Show system hardware stats (CPU, RAM, disk, temperature, network, uptime). |
| `/wishlist-item <url>` | Add a link to track price and stock. Checked every 12 hours. Currency is read from the page (JSON-LD / meta tags) with a TLD-based fallback for `.dk` → DKK and `.ro` → RON. Once ~3 days of history have accumulated **and** the price has moved by at least ~1 % over the tracked window, the bot will also DM you 🟢 **buy-window** alerts (price at the all-time low in the rolling 180-day window) and 🔴 **wait** alerts (price above the historical median). Perfectly-flat prices stay quiet to avoid spurious "all-time low" DMs. |
| `/wishlist-item-delete <url>` | Remove a link and its history from tracking. |
| `/wishlist-show [currency]` | Show your tracked items and their current prices. By default each row is shown in its own native currency (merchant-quoted or TLD-guessed). Optional `currency` (RON, DKK, EUR, USD, GBP) converts every row into that single currency instead. |
| `/wishlist-graph <url> [currency]` | Price evolution graph for one tracked URL. Defaults to the item's own currency (no conversion). Optional `currency` (RON, DKK, EUR, USD, GBP) converts the Y-axis into that unit instead. |
| `/wishlist-graph-all [currency]` | Combined price evolution graph for **all** your tracked items, normalized to a single currency so cross-currency items share one Y-axis. Defaults to the **majority currency** across your tracked items (largest number of items shown without conversion). Optional `currency` (RON, DKK, EUR, USD, GBP) overrides the default. |
| `/help` | Show the help message. |

---

## 📁 Project Structure

### Top-level

| File | Description |
|---|---|
| `bot.py` | Thin entry point — instantiates every feature class, registers a unified `on_message` dispatcher and `on_ready` task starter, then runs the Discord client. |
| `api.py` | Flask REST API for managing responses, reminders, jokes, and wishlist items externally. `POST /wishlist/add` does a live scrape during the request so unscrapeable URLs are rejected immediately instead of becoming dead rows. |
| `scraper.py` | Pure scraping core — `PriceScraper`, `ScrapeResult`, URL/TLD helpers. No Discord or Matplotlib imports, so `api.py` can reuse it without dragging those into the API process. The Discord-side `ScrapingFeature` re-imports from here. |
| `db.py` | Database layer — all SQLite queries and a shared connection context manager. |
| `logger.py` | Shared logging setup used by both the bot and the API. Uses `RotatingFileHandler` (5 MB × 2 backups) and attaches the `database` and `scraper` module loggers to the same handlers. |
| `requirements.txt` | Python runtime dependencies. |
| `requirements-dev.txt` | Test-only dependencies (pytest). |
| `pytest.ini` | Pytest configuration (test discovery rooted at `tests/`). |
| `tests/` | Pytest suite — see [Running Tests](#-running-tests). |
| `.env` | Environment variables (ignored by git). |
| `responses.db` | SQLite database file. Auto-created on first run, **gitignored** (not tracked), and relocatable via the `DB_FILE` env var — see [Configuration](#3-configuration). |

### `features/` — one class per domain

Each feature class self-registers its slash commands in `__init__`, optionally implements `handle_message(message)` to participate in the `on_message` chain, and optionally implements `start_tasks()` to kick off background loops in `on_ready`.

| File | Class(es) | Responsibility |
|---|---|---|
| `response_gate.py` | `ResponseGate` | Shared cooldown clock used by `KeywordsFeature` so the bot never spams the channel. Exposes `can_respond()` / `mark_responded()` and a `DEFAULT_COOLDOWN_SECONDS` default. |
| `keywords.py` | `KeywordsFeature` | Owns keyword auto-responses (`on_message` match → reply), `/keyword_add`, and `/topkeywords`. Consults `SponsorsFeature` to append a sponsor suffix and consumes the shared `ResponseGate`. |
| `teases.py` | `TeasesFeature` | Random mood-based teases that fire on messages, plus the `/mood` command. Owns the full `TEASE_MOODS` mood-line corpus and the daily tease counter. |
| `inactivity.py` | `InactivityFeature` | Tracks the most recent activity timestamp per guild via `handle_message`, and nudges quiet channels with an `INACTIVITY_MESSAGES` line via a 30-minute background loop. |
| `reminders.py` | `RemindersFeature` | The `/remind` command (parses `30m` / `2h` / `1d`) and a 10-second loop that delivers due reminders and deletes them from the DB. |
| `jokes.py` | `JokesFeature` | The `/joke_add` and `/joke_activation` commands plus a 30-second loop that posts one unsent joke per day at the configured time and recycles the pool when exhausted. |
| `sponsors.py` | `SponsorsFeature`, `_SponsorModal` | Sponsor state machine (name, tier, custom message, set-at timestamp), `/sponsor_set` (gated by a password modal), `/sponsor_plans`, `/sponsor_who`, and an hourly loop that warns 1 day before expiry and clears the sponsor after 1 year. Exposes `maybe_get_sponsor_suffix()` to other features. |
| `scraping.py` | `ScrapingFeature`, `PriceScraper`, `CurrencyConverter` | All `/wishlist-*` commands plus a 12-hour scrape loop that DMs users on price/stock changes and trims price history older than `PRICE_HISTORY_RETENTION_DAYS` (default 180 days). After each scrape pass `_classify_price` decides whether to fire a 🟢 LOW (current ≤ all-time-low; re-fires only on a ≥1 % further drop) or 🔴 HIGH (current > median; one alert per elevated period, re-arms after returning to/below median) buy-signal — both rolled into the same DM as the price-change / back-in-stock notifications, with two guardrails so freshly-added items don't spam: an `ALERT_MIN_DATA_POINTS` (7) data-points floor *and* a variance check that requires the overall observed spread (history + current) to be at least `ALERT_LOW_REALERT_DROP_PCT` (~1 %) wide before any zone is considered meaningful. `PriceScraper` extracts price/title/currency/stock from JSON-LD → meta → text-fallback, then guesses currency from the URL's TLD (`TLD_CURRENCY_FALLBACKS`: `.dk` → DKK, `.ro` → RON) when the page provides no currency metadata. `CurrencyConverter` refreshes exchange rates daily and pivots through EUR for display (matching the API's native base — stored rates are "units per 1 EUR"); the supported display set (`SUPPORTED_DISPLAY_CURRENCIES` = RON / DKK / EUR / USD / GBP) drives the `currency` picker on `/wishlist-show`, `/wishlist-graph`, and `/wishlist-graph-all`. Uses Matplotlib (Agg backend) with `ConciseDateFormatter` on the time axis so charts stay readable across both short and multi-month ranges. Optional `curl_cffi` dependency impersonates Chrome's TLS fingerprint to bypass bot-detection on protected sites (Altex, eMag, Cel.ro); falls back to plain `requests` when unavailable. |
| `stats.py` | `StatsFeature` | The `/stats` command — host CPU, RAM, disk, temperature, network, uptime and bot process memory via `psutil`. Gracefully degrades load average to `N/A` on Windows. |
| `help_feature.py` | `HelpFeature` | The `/help` command — static description of all user-facing slash commands. |
