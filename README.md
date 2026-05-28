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
sudo apt install python3-flask python3-requests python3-bs4 python3-matplotlib python3-psutil python3-dotenv
```
*Note: You might still need to install `discord.py` via pip: `pip3 install discord.py`.*

### 3. Configuration 
Create a `.env` file in the root folder and add your credentials:
```env
DISCORD_TOKEN=your_token_here
HOST=your_host_here
PORT=your_port_here
```

---

## 🏃 Execution
This project runs as two separate processes (the bot and the API). Use PM2 to keep them running in the background.

**Starting the Bot**
```bash
pm2 start bot.py --interpreter python3 --name discord-bot
```
**Starting the API**
```bash
pm2 start api.py --interpreter python3 --name discord-api
```

**Useful PM2 Commands**

* `pm2 status` — Check if the bot and API are online.
* `pm2 logs` — View real-time logs and errors.
* `pm2 restart all` — Restart both processes.
* `pm2 stop all` — Stop the bot and API.

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
| `/scrape-item <url>` | Add a link to track price and stock. Checked every 12 hours. |
| `/scrape-item-delete <url>` | Remove a link and its history from tracking. |
| `/scrape-show` | Show your tracked items and their current prices. |
| `/scrape-graph <url>` | Show a price evolution graph for one tracked URL. |
| `/scrape-graph-all` | Combined price evolution graph for **all** your tracked items, normalized to DKK so cross-currency items share one Y-axis. |
| `/help` | Show the help message. |

---

## 📁 Project Structure

### Top-level

| File | Description |
|---|---|
| `bot.py` | Thin entry point — instantiates every feature class, registers a unified `on_message` dispatcher and `on_ready` task starter, then runs the Discord client. |
| `api.py` | Flask REST API for managing responses, reminders, and jokes externally. |
| `db.py` | Database layer — all SQLite queries and a shared connection context manager. |
| `logger.py` | Shared logging setup used by both the bot and the API. |
| `requirements.txt` | Python dependencies. |
| `.env` | Environment variables (ignored by git). |
| `responses.db` | SQLite database file (auto-created on first run). |

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
| `scraping.py` | `ScrapingFeature`, `PriceScraper`, `CurrencyConverter` | All `/scrape-*` commands plus a 12-hour scrape loop that DMs users on price/stock changes. `PriceScraper` extracts price/title/currency/stock from JSON-LD → meta → text-fallback. `CurrencyConverter` refreshes exchange rates daily and pivots through DKK for display. Uses Matplotlib (Agg backend) to render price-history graphs. |
| `stats.py` | `StatsFeature` | The `/stats` command — host CPU, RAM, disk, temperature, network, uptime and bot process memory via `psutil`. Gracefully degrades load average to `N/A` on Windows. |
| `help_feature.py` | `HelpFeature` | The `/help` command — static description of all user-facing slash commands. |
