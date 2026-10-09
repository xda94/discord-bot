# Discord Keyword Responder Bot

A Python Discord bot with keyword auto-responses, mood-based teases, reminders, recurring birthday greetings, per-server daily jokes, sponsorship tags, bounded natural-language commands, global per-user assistant profiles, automatic or user-managed per-user LLM memory, image-aware mention replies, a **wishlist** price tracker (scrape loop, DMs on price/stock changes, buy/wait signals, and history graphs), and a per-user **flight price tracker**. A separate **Flask API** manages the same data from scripts or other tools. Both processes share one SQLite database and are typically kept alive with **PM2**.

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

**Why two Python entry points?** `wishlist/scraper.py` holds pure HTTP/HTML parsing with no Discord or chart-renderer imports. The API and `features/wishlist.py` reuse the same wishlist services while keeping Discord commands and notifications in the bot process.

---

## Getting started

### Prerequisites

- Python 3.x and pip
- SQLite3 (usually bundled with Python)
- Node.js + npm (for PM2)
- `llama-server` with a compatible GGUF instruct/chat model
- For graphs: `vl-convert-python` (installed from `requirements.txt`)

### Install dependencies

```bash
npm install pm2 -g && pm2 update
pip install -r requirements.txt
```

With a venv, use `./venv/bin/pip` instead of `pip`.

Wishlist graphs use the self-contained Vega-Lite renderer from `vl-convert-python`; PNG generation does not require Chrome or an external chart service.

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
LLM_MENTION_MAX_TOKENS=384
LLM_MEMORY_ENABLED=0
```

| Variable | Required | Notes |
|---|---|---|
| `DISCORD_TOKEN` | Yes (bot) | Bot refuses to start without it. |
| `BOT_ID` | Yes (bot) | Your bot's Discord user ID (Developer Mode → right-click bot → Copy User ID). Used to recognize @mentions addressed to the bot. |
| `HOST` | Yes (API) | Bind address. Use `0.0.0.0` for LAN/Tailscale or **Docker** (published ports). Use `127.0.0.1` only if the API should be local to the host (e.g. PM2, no remote access). |
| `PORT` | Yes (API) | e.g. `9999`. |
| `API_TOKEN` | Required (API data routes) | Data/configuration routes require `Authorization: Bearer <token>` and return HTTP 503 when unset. Dashboard/static assets and `/health` are public. |
| `SPONSOR_PASSWORD` | No (bot) | Password required by the `/sponsor-set` and `/sponsor-set-tiers` Discord modals. Dashboard tier writes use `API_TOKEN` instead. |
| `DB_FILE` | No | Full path to the SQLite file (filename included), e.g. `/var/lib/discord-bot/responses.db`. Default: `responses.db` in the working directory. Parent dirs are created automatically. |
| `LOG_LEVEL` | No | Logging verbosity for console and rotating files. Default: `INFO`; use `DEBUG` to include per-observation memory capture metadata. |
| `LLAMA_CPP_BASE_URL` | No (bot) | `llama-server` base URL. Default: `http://127.0.0.1:8080`. Docker defaults to `http://host.docker.internal:8080`. A URL ending in `/v1` is also accepted. |
| `LLAMA_CPP_DEFAULT_MODEL` | Yes (bot) | Default model alias passed to `llama-server`. Must be listed in `LLAMA_CPP_ALLOWED_MODELS`. Match the alias supplied to `llama-server --alias`. |
| `MENTION_LLAMA_CPP_MODEL` | No (bot) | Model alias for @bot mentions. Defaults to `LLAMA_CPP_DEFAULT_MODEL` and must be allowed. |
| `LLAMA_CPP_ALLOWED_MODELS` | Yes (bot) | Comma-separated llama.cpp model aliases offered by `/llm-set`. A single-server setup normally lists one alias. |
| `NATURAL_LLM_ENABLED` | No | Defaults to disabled. Set `1` only after the deployed model passes the read-only bilingual corpus evaluation; deterministic mention commands remain available. |
| `LLAMA_CPP_TIMEOUT` | No | Default capacity queue deadline and HTTP generation timeout, in seconds. Default: `180`; the HTTP timeout begins after admission activates. |
| `LLM_CAPACITY_DIR` | No | Shared POSIX lock directory for bot/API LLM capacity. Defaults to `.llm-capacity` beside `DB_FILE`; Compose uses `/data/.llm-capacity` for both services. |
| `LLAMA_CPP_API_KEY` | No | Optional bearer token when `llama-server` is configured to require an API key. |
| `ASK_COOLDOWN_SECONDS` | No (bot) | Per-user cooldown for mentions after each answer finishes. Default: `60` (1 minute). |
| `LLM_CONTEXT_MESSAGES` | No (bot) | Maximum number of recent live channel messages considered for mentions. Synthesized memory and live context share a 6,000-character budget (4,000 for vision). Default: `0`. Set it to `5` so the bot can answer questions about recent messages and pick up the channel's language for very short questions; every extra message makes each reply slower on a CPU-only host. |
| `LLM_MENTION_MAX_TOKENS` | No (bot) | Maximum output tokens for one @mention reply. Default: `384`; minimum: `1`. With `LLM_MENTION_THINKING=1` this budget also covers the hidden reasoning, so leave room for llama-server's `--reasoning-budget`. |
| `LLM_MENTION_THINKING` | No (bot) | `1` lets thinking models reason before @mention replies; every other request (teases, reactions, summons, birthdays, memory) always disables thinking. Requires a bounded `--reasoning-budget` on llama-server. Improves answers about chat history at the cost of slower replies. Default: off. |
| `LLM_MEMORY_DISABLED` | No (bot) | `1` turns LLM memory off entirely: no `/memory-*` or `/llm-memory*` commands, no automatic capture, and no saved memory in mention prompts (yours or other people's). Overrides `LLM_MEMORY_ENABLED`. Saved notes add a few hundred tokens to every mention, which on a CPU-only host is often the largest part of the reply time. Default: off. |
| `LLM_MEMORY_ENABLED` | No (bot) | Memory mode selected at startup. Only `1` enables automatic channel capture and synthesis. `0`, a missing value, or an invalid value uses manual memory through `/memory-add`, `/memory-show`, and `/memory-erase`. Restart the bot after changing it. |
| `LLM_MEMORY_CONSOLIDATION_INTERVAL_SECONDS` | No (bot) | Automatic mode only. Interval between scans that may start new memory cycles and cap for consecutive-failure backoff. Default: `300` (5 minutes); minimum: `1`. A new channel cycle needs 50 captured, permitted messages that are each at least 600 seconds old. |
| `LLM_MEMORY_ACTIVE_CHUNK_REST_SECONDS` | No (bot) | Automatic mode only. Rest between successful chunks and base interval for consecutive-failure backoff while a memory cycle is active. Default: `300` (5 minutes); minimum: `1`. |
| `LLM_MEMORY_MAX_TOKENS` | No (bot) | Automatic mode only. Maximum output tokens for one memory extraction. Default: `1024`; minimum: `1`. |
| `LLM_REACTION_CHANCE` | No (bot) | Chance that an eligible ordinary message is considered for one contextual emoji reaction. Default: `0.10`. |
| `LLM_REACTION_COOLDOWN_SECONDS` | No (bot) | Shared per-channel cooldown for contextual reactions. Default: `60`. |
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
  -hf unsloth/gemma-4-E4B-it-GGUF:UD-Q4_K_XL \
  --no-mmproj-offload \
  --alias discord-bot \
  --host 127.0.0.1 \
  --port 8080 \
  --ctx-size 4096 \
  --parallel 1 \
  --threads 3 \
  --threads-batch 3 \
  --swa-full \
  --cache-reuse 64
```

On a four-core host, three inference threads leave one core available for the
OS, Discord bot, and API. Four inference threads can make the machine
unresponsive even though the bot's event loop itself is not blocked.

Keep `--ctx-size` at `4096` or more (per slot, when `--parallel` is above 1). A
mention prompt is about 250 tokens of instructions; with
`LLM_CONTEXT_MESSAGES=5` and typical chat it grows to roughly 450 tokens, but
with the full 6,000-character memory and history budget it reaches about 2,300
tokens. A 2048-token context then rejects the request and the mention reply
fails.

On a CPU-only host, reading the prompt takes most of the reply time (an Intel
N150 with three threads reads about 11 tokens per second). Set
`LLM_CONTEXT_MESSAGES` to `5`: enough for questions about recent messages, at
about 100 tokens of typical chat. Saved memory notes are usually the largest
part of the prompt (20 notes add roughly 250–500 tokens), so on a slow host consider
`LLM_MEMORY_DISABLED=1`.

`--swa-full --cache-reuse 64` let every mention reuse the already processed
instructions (about 250 tokens) instead of reading them again; Gemma 4's
sliding-window attention otherwise discards that cache. The mention prompt
starts with these fixed instructions, followed by everything that differs per
request. `--swa-full` costs about 120 MB of extra memory at a 4096-token
context.

The bot calls llama.cpp's OpenAI-compatible `/v1/chat/completions` endpoint.
Gemma's matching multimodal projector is loaded automatically by `-hf`;
`--no-mmproj-offload` keeps the projector on the CPU-only host. Do not use
`--no-mmproj`, which disables vision. Before starting the bot, confirm the
server reports `"vision": true` under `modalities`:

```bash
curl -s http://127.0.0.1:8080/props
```

See the official llama.cpp [multimodal documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md)
and [server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
for model/projector and endpoint details.

Requests are user-only by default. Mention answers and empty-ping summons add a
short, mention-specific `system` instruction that names the bot (its live server
nickname, so no name is configured in the repo) and asks for direct final output;
other generators remain user-only. Every request also sends
`chat_template_kwargs: {"enable_thinking": false}` (except @mention replies when
`LLM_MENTION_THINKING=1`), because thinking models such as Gemma 4 otherwise spend
short output budgets on hidden reasoning and the reply fails. These settings do not guarantee compliance from every model or chat
template. If multiple aliases are listed, each one must be reachable
through the configured endpoint (for example through a compatible model router).

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

After `git pull`, restart both if either the `db` package schema or slash commands changed. The bot syncs slash commands on `on_ready`; the first deployment containing `/sponsor-set-tiers` therefore needs the normal bot restart/startup sync. Later tier edits are persisted immediately and need neither a bot restart nor another command sync.

---

## Local dashboard

The API process serves a responsive administration dashboard at
`http://<mini-pc-ip>:<PORT>/`. It uses the existing REST API and has no build
step or separate frontend process. For direct access from a trusted local
network, use:

```env
HOST=0.0.0.0
PORT=9999
API_TOKEN=choose-a-long-random-secret
```

Restart `discord-api` after changing those values, then open the mini PC's LAN
IP from a phone or computer on the same network and sign in with the same
`API_TOKEN`. The token is stored only in that browser tab's session storage,
sent as an `Authorization: Bearer` header, and removed on logout or an
unauthorized response. The dashboard manages
keywords, reminders, birthdays, jokes, wishlist items, flight trackers, and bot settings.
It also manages persistent LLM memory channels and user controls, and shows
saved keyword/LLM/price analytics plus live mini PC CPU, temperature, memory,
disk, and uptime metrics. Server and user IDs select records; they are not an
authentication mechanism. Under Bot settings it also manages the global sponsor
tier catalog: create or edit tier name, annual price in lei, and append chance
as a percent (fractional percentages are supported). Editing a tier immediately
changes the plan display, active-tier probability, and status text without
renewing the active sponsorship or changing its expiry. The dashboard never
asks for the Discord sponsor password; its tier routes use the bearer token.

The HTML and static assets remain loadable so the login screen can open. All
dashboard data and mutations use the existing bearer-protected REST routes.

---

## Background tasks

| Feature | Interval | What it does |
|---|---|---|
| Wishlist scrape | 12 h | Fetches each tracked URL, updates price/stock, appends history, trims entries older than 180 days, DMs on change / back-in-stock / buy-wait alerts |
| Flight tracker | 5 h (configurable) | Checks the least-recently searched fixed-date tracker for each user, stores price history, and DMs on the first result or a lower price |
| Exchange rates | 24 h | Refreshes EUR-based rates for RON, DKK, EUR, USD, GBP |
| Daily joke | 30 s check | Per subscribed guild: posts one joke in the configured window once per day |
| Birthday check | Daily at 00:01 | Sends one LLM-generated annual birthday greeting per saved user; failures retry every 30 minutes during the date |
| Reminders | 10 s | Claims due occurrences, delivers/retries, and advances recurrence |
| Tracking notifications | 30 s | Drains the durable outbox, respecting opt-in quiet hours and daily digests |
| Inactivity nudge | 30 min | Nudges quiet guild channels where `/llm-inactivity` is activated |
| Sponsors | 1 h | Expiry warning and cleanup |
| Teases | On message | Random mood lines rewritten via llama.cpp |

---

## Bot commands

### Keywords and chat

| Command | Description |
|---|---|
| `/keyword-add <keyword> <response>` | Add a keyword → response pair **for this server only** (random pick when multiple). |
| `/top-keywords [user]` | Most triggered keywords in the server. |
| `/mood <mood>` | Set tease mood; random teases are rewritten via llama.cpp in that style and in the triggering message's language. |
| `/help` | Localized categories and examples, with a complete command reference button. |
| `/start` | Guided private language → optional timezone picker → ready flow, with feature navigation. |
| `/natural-language` | Read-only English/Romanian examples guide for supported mention commands. |

### Reminders

| Command | Description |
|---|---|
| `/remind <when> <who> <what> [recurrence]` | Duration or calendar reminder, with optional daily, weekdays, or weekly recurrence. |
| `/reminder-list` | Private lifecycle list and owner-checked edit/cancel/retry controls. |
| `/reminder-edit <reminder-id>` | Edit reminder text and local time. |
| `/reminder-cancel <reminder-id>` | Cancel a saved reminder. |
| `/reminder-snooze <reminder-id>` | Create an independent ten-minute one-off. |

### Assistant profile and natural commands

Romanian commands, calendar reminders, reminder management, flight budgets, durable quiet-hour/digest notifications, bilingual dashboard controls, additive migrations, and rollout verification are documented in [Bilingual commands and delivery](docs/romanian-commands.md).

`/start` guides you through English, Română, or Automatic, then a timezone
dropdown and a ready screen in one private message. Choose a common timezone
or browse all validated IANA locations by region and page; no typing is needed.
Timezone setup is optional: Skip preserves any saved timezone and writes
nothing. Relative reminders work without a confirmed timezone; calendar and
recurring schedules require one, including when your choice is UTC. Language
and timezone changes preserve your other preferences, and reopening `/start`
begins with your current language setting. Controls belong to the requester
and expire after ten minutes.

`/natural-language` opens a private, read-only guide with English/Romanian
controls that change only the guide's display language. Its categories cover
relative/calendar/recurring reminders and their management, wishlist tracking
and alerts, refreshes and price graphs (1–180 days), and listing saved flight
trackers. Flight creation and management use the existing slash commands.
Replace the example URLs and IDs with your own, and select the real bot account
for every mention request, including DMs and clarification replies. Supported
actions execute immediately and acknowledge success with ✅; lists and graphs
arrive privately. If DMs are blocked, use the displayed private slash
alternative. Missing details open a ten-minute clarification. The same guide
is available from `/start` and `/help`.

The new command becomes available after the bot's normal startup
`on_ready` command sync; restart the bot after deploying this change.

The implementation and runtime architecture are also summarized in
[`bot.md`](bot.md), including event routing, persistence, delivery recovery,
API routes, and deployment notes.

### Current implementation status

The repository now includes the deterministic Romanian/English command parser,
shared localization catalogs, timezone-aware duration/calendar parsing,
clarification and ownership controls, lifecycle-managed reminders, recurrence,
retry/reconciliation, durable tracking notifications, wishlist IDs and target
controls, flight budgets and airport autocomplete, guided `/start`, the
read-only bilingual `/natural-language` guide, categorized help,
the independent bilingual dashboard selector, and aggregate natural-command
analytics. SQLite changes are additive and existing slash/API contracts remain
compatible.

The reviewed parser corpus contains 148 English/Romanian cases and currently
passes at 100% with zero unauthorized actions. Regression coverage includes
guided setup and every concrete example published in the natural-language
guide. `NATURAL_LLM_ENABLED` remains disabled by default until a deployed
model passes the read-only 100-case evaluation gate; the live model endpoint
and a Discord test server still need to be exercised before enabling generated
action execution.

| Command | Description |
|---|---|
| `/assistant-profile` | Privately show your global assistant preferences. Reading defaults does not create a database row. |
| `/assistant-profile-set [language] [tone] [currency] [timezone] [notification-style] [llm-behavior]` | Privately update only the supplied fields. Currency can be unset; timezone must be an IANA name such as `Europe/Bucharest`. |
| `/assistant-profile-reset` | Delete your stored profile and return to defaults. |

Profile defaults are language `auto`, tone `default`, no currency override,
timezone `UTC`, notification style `standard`, and LLM behavior `balanced`.
Profiles are global per Discord user and independent from conversational memory.
Language, tone, and answer detail are snapshotted when a text, vision, or empty
summon mention is queued; an explicit language request in the current message
wins over the saved language. A saved currency is used only when a supported
wishlist/flight command omits currency. `compact` shortens scheduled wishlist
and flight DMs. Calendar reminders, recurrence, quiet hours, and daily digests
require an explicitly configured timezone; relative one-off durations do not.
Tracking delivery defaults to immediate with no quiet hours. The dashboard
language preference is independent of the selected Discord user’s profile.

When the bot is explicitly mentioned, it recognizes these complete English or
Romanian phrases before falling through to the ordinary mention LLM:

| Operation | English | Romanian |
|---|---|---|
| Track | `track <URL>` (or reply with `track this`) | `urmărește <URL>` (or reply with `urmărește asta`) |
| Flights | `show my flights` | `arată-mi zborurile mele` |
| Show wishlist | `show my wishlist [in EUR]` | `arată-mi wishlist-ul [în EUR]` |
| Remove | `stop tracking <URL>` | `nu mai urmări <URL>` |
| Target | `set target price for <URL> to 100 EUR` | `setează prețul țintă pentru <URL> la 100 EUR` |
| Clear target | `clear target price for <URL>` | `șterge prețul țintă pentru <URL>` |
| Restock only | `enable|disable restock only for <URL>` | `activează|dezactivează doar notificările de stoc pentru <URL>` |
| Refresh | `refresh <URL>` / `refresh my wishlist` | `actualizează <URL>` / `actualizează wishlist-ul meu` |
| Graph | `graph <URL> [in EUR] [for 30 days]` or `graph my wishlist [in EUR] [for 30 days]` | `grafic pentru <URL> [în EUR] [pentru 30 zile]` or `grafic pentru wishlist-ul meu [în EUR] [pentru 30 zile]` |
| Compare | `compare my wishlist [for 30 days]` | `compară wishlist-ul meu [pentru 30 zile]` |
| Reminder | `remind me in two hours to stretch` | `amintește-mi peste 2 ore să sun acasă` |

Durations accept positive numbers, common written values, fractions, weeks,
and compounds such as `o oră și 30 de minute`. Graph periods are whole days
from 1 to 180. Complete actions execute immediately; missing or ambiguous fields
start a ten-minute clarification. Every text follow-up requires a bot mention.
Successful actions and requested private deliveries receive only ✅.

Flight, wishlist, reminder, refresh, and graph results stay private. If DMs fail,
the response names the exact slash command for an ephemeral alternative.
Wishlist references accept owned IDs, titles, URLs, or unambiguous bot results.
Refresh preserves existing cooldowns and saved-history graphs. Unmatched mentions
use the existing model worker; structured model execution is opt-in through
`NATURAL_LLM_ENABLED=1` after the read-only evaluation passes. See the bilingual
delivery guide above for migrations, limitations, and rollout checks.

### Birthdays

| Command | Description |
|---|---|
| `/set-birthday [date]` | Save your birthday as `DD.MM`, `DD-MM`, or `DD/MM` for an annual LLM-generated greeting in the channel where it is set. Omit `date` to delete it. |

Each Discord user has one saved birthday across the bot. Saving it again updates
the date and delivery destination, including a DM channel. Confirmations are
private. The first attempt runs at 00:01 using the bot process's local date,
with only the registered user mentioned. February 29 is celebrated only in leap
years. Failed generation or delivery is retried every 30 minutes during that
date, but a successful greeting is not repeated and no belated greeting is sent.

### Daily jokes (per server)

The joke **pool** is global; **schedule and “already sent” history** are per guild.

| Command | Description |
|---|---|
| `/joke-add <text>` | Add text to the shared pool. |
| `/joke-activation <time>` | Enable daily joke in **this channel** at `HH:MM` (e.g. `14:00`). Each server configures independently. |
| `/joke-deactivation` | Disable for this server (sent history kept). |
| `/joke-status` | Ephemeral: channel, time, last sent date, or not activated. |

On first boot after upgrading from single-guild jokes, the bot migrates the old global channel/time settings into one `guild_joke_config` row automatically.

### Sponsors

| Command | Description |
|---|---|
| `/sponsor-set [user] [plan]` | Password modal; optional plan tier and custom message (top tier). |
| `/sponsor-set-tiers [plan]` | Password modal to create a tier or edit the selected tier's name, annual price in lei, and chance percent. |
| `/sponsor-plans` | Plans, prices, append chance on keyword replies. |
| `/sponsor-who` | Current sponsor and time until 1-year expiry. |

Sponsor configuration is global across every server served by the bot. The
catalog starts with the four built-in IDs (`standard`, `entuziast`, `premium`,
and `ultra`) and is stored in the shared SQLite `settings.sponsor_tiers` JSON
setting as versioned default overrides plus custom tiers. Custom tier IDs are
stable after creation. Existing active sponsorships retain their persisted
tier ID, start time, warning flag, and duration; an unknown active ID uses the
standard tier's probability/display fallback. Only the stable `ultra` ID grants
the existing custom-message suffix, even if its display name is edited. The
Discord modal accepts chance as 0–100 percent; the API and database contract
use a 0–1 probability.

### Wishlist (price tracking)

| Command | Description |
|---|---|
| `/wishlist-item <url>` | Track URL; live scrape on add. Checked every 12 h; DMs on price/stock changes. |
| `/wishlist-item-delete <url>` | Remove item and its price history. |
| `/wishlist-target-price <url> <price> <currency>` | Notify once when an item reaches the configured price or lower; target currency may differ from the shop currency. |
| `/wishlist-target-clear <url>` | Remove one target-price alert. |
| `/wishlist-restock-only <url> <enabled>` | Suppress price/target DMs for this item while continuing to track it; only back-in-stock changes notify. |
| `/wishlist-refresh [url]` | With a URL, fetch only that tracked item; omit it to refresh your entire wishlist. Reports source, freshness, stock, price, and target progress. Five-minute cooldown per item; cooling items are skipped during an all-item refresh. |
| `/wishlist-show [currency]` | List your items. Default: saved profile currency when set, otherwise each item’s native currency. Optional: `RON`, `DKK`, `EUR`, `USD`, `GBP`. |
| `/wishlist-graph <url> [currency] [days]` | PNG price history for one URL. Omitted currency uses the saved profile currency, then the item's native currency. Any whole-number period from 1–180 days; default 180. |
| `/wishlist-graph-all [currency] [days] [percentage]` | Combined graph; omitted currency uses the saved profile currency, then the majority across your list. Set `percentage:true` to compare changes from each product's first observation in the selected period. |

Graph replies have **7 / 30 / 90 days** buttons and a **Custom days** dialog
(1–180). For example, `/wishlist-graph-all days:45 percentage:true` compares
45 days of relative price changes. The combined graph also has a button to
switch between prices and percentage changes. Percentage mode starts each
product at 0%; products with a zero or negative starting price are skipped.
It uses each product's native observations and needs no exchange rates.

Controls belong to the requester and expire after ten minutes of inactivity
or a bot restart. They redraw saved data without fetching product pages.
An empty period keeps the controls available to select a longer range.
Charts use straight lines between observations, unique numbered product labels,
and date/time labels in UTC that adapt to the history span. Custom periods
cannot recover history already removed by the 180-day retention policy.

**Currency** — read from the page when possible; TLD fallback (e.g. `.ro` → RON, `.dk` → DKK).

**Text-fallback extraction** — If JSON-LD or meta tags are missing, the scraper strips `<script>` and `<style>` tags to check visible page text for stock status keywords, preventing false "out of stock" readings triggered by hidden JS localization strings.

**AI Fallback Scraping** — When both price and stock remain unknown, the scraper uses local llama.cpp (`LLAMA_CPP_DEFAULT_MODEL`) to extract them from visible page text, even when a title or currency exists. Usable data requires a finite nonnegative price (including zero) or explicit stock status. Boolean, negative, NaN, and infinite prices are rejected; blank titles/currencies become missing, and unknown availability stays unknown. Fallback fills missing fields, and TLD currency guessing runs afterward.

Manual add/refresh fallback uses interactive LLM capacity. If capacity is unavailable, Discord asks the user to retry and the API returns HTTP 503. Scheduled fallback skips under contention and retries on the next scrape pass. Failed refreshes preserve saved prices, stock, metadata, alert state, and history while recording the check outcome. Successful snapshots, check state, and history commit together; database failures are reported as failures. Target/restock preference changes are validated together and saved in one write. Currency conversion loads a shared rate snapshot once per minute instead of reading SQLite for every conversion.

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
| `/flight-tracker-add <origin> <destination> <start-date> <end-date> [adults] [currency] [budget]` | Save a fixed-date round-trip watch and run its first search immediately. Omitted currency uses the saved profile currency, then EUR. On first use, opens the private SerpApi login modal. Use airport/city autocomplete or three-letter IATA codes and `YYYY-MM-DD`. |
| `/flight-tracker-budget <tracker-id> [budget]` | Set or clear a budget threshold in the tracker’s currency. |
| `/flight-tracker-show` | List only your trackers, IDs, periods, latest best dates/prices, and any provider error. |
| `/flight-tracker-delete <tracker-id>` | Delete only your own tracker and its price history. |
| `/flight-tracker-login` | Validate and set/replace your own SerpApi API Key. |
| `/flight-tracker-logout` | Remove your saved SerpApi key. Existing trackers remain saved but checks pause until the next login. |

Exact example: `/flight-tracker-add origin:OTP destination:BKK start-date:2026-12-30 end-date:2027-01-13`.

Only fixed departure and return dates are supported. Flexible date windows were intentionally removed because each date pair would consume a separate SerpApi search and quickly exhaust the free quota.

Each Discord user supplies one **SerpApi API Key**. If no login exists when `/flight-tracker-add` is used, the bot opens a modal, validates the key through SerpApi's Account API, saves it, and then creates the tracker. Later adds reuse that same user's key and quota. Keys are stored in the local SQLite database and are never shown by commands or written to logs. Because SQLite storage is not encrypted, filesystem/database access must be restricted to the bot operator; use `/flight-tracker-logout` to remove a user's key.

The free SerpApi plan currently includes 250 searches per month. To stay below that, each five-hour scheduled pass checks at most one tracker per user/API key, selecting the least-recently checked active tracker. Departed and retired flexible trackers are excluded before selection. This caps scheduled usage at about 144 successful searches per user in a 30-day month regardless of how many trackers are saved; multiple trackers rotate and are therefore checked less often individually. Immediate searches performed by `/flight-tracker-add` use additional credits from the remaining headroom. See the [SerpApi Google Flights documentation](https://serpapi.com/google-flights-api) and [pricing](https://serpapi.com/pricing).

Successful flight observations, price history, budget crossing state, and any new notification commit in one SQLite transaction. An enqueue failure leaves the previous observation intact for retry. Accepted notifications survive temporary DM or database failures in the durable outbox.

### System

| Command | Description |
|---|---|
| `/stats` | Portable Windows/Linux/macOS host stats: platform, CPU/cores, RAM, current drive/filesystem, network, uptime, and bot memory. Temperature/load show `N/A` when the host does not expose them. |
| `/llm-set <model>` | Set the allowed llama.cpp model alias used when the bot is mentioned. **60s cooldown** per user for mentions. |
| `/llm-inactivity <activate\|deactivate>` | Enable or disable LLM-generated inactivity nudges for this server. Requires **Manage Server** permission. Existing servers default to enabled. |
| `/llm-memory <activate\|deactivate\|status>` | Automatic mode (`LLM_MEMORY_ENABLED=1`) only. Manage memory capture in the current channel; requires **Manage Server**. |
| `/llm-memory-purge <confirmation>` | Automatic mode only. Delete all saved memory for this server by entering `PURGE`. Channel settings and user opt-outs are preserved. |
| `/memory-add <type> <text>` | Manual mode only. Save up to 200 characters as Likes, Dislikes, Facts, Interests, Opinions, or Other in the current server or DM. |
| `/memory-show` | Privately show your complete saved memories, grouped by type, in the current server or DM. Available in both modes. |
| `/memory-erase [text]` | Manual mode only. Paste complete stored text to erase its matches, ignoring case and repeated whitespace; omit `text` to erase everything here. |
| `/memory-forget` | Automatic mode only. Erase synthesized entries and pending observations here without opting out. |
| `/memory-opt-out` | Automatic mode only. Stop memory and erase all of your memory data in the current server or DM. |
| `/memory-opt-in` | Automatic mode only. Re-enable memory for you; required before automatic memory can operate in DMs. |
| `@bot` | Replies in-thread and tags the requester once. Empty ping → short prompt back; with text → one direct LLM answer. |
| `@bot <text>` | Uses `MENTION_LLAMA_CPP_MODEL` and the configured recent context to resolve brief questions; returns one ready-to-send reply in the current message's language rather than response options. |
| `@bot` + image | Inspects the first processable directly attached JPEG, PNG, GIF, WebP, BMP, or TIFF. With a caption it answers that request; without one it gives a concise description. |
| `/llm-feedback-summary` | Manage Server only; compare this server’s rated reply configurations. |

Vision requests accept JPEG, PNG, GIF, WebP, BMP, and TIFF directly attached to
the mention, using the first image that can be processed in attachment order.
Animated images and multipage TIFFs use only the first frame or page. The actual
image bytes determine the format; valid static JPEG/PNG files are preserved, and the
other supported formats are normalized locally to RGB/RGBA PNG. Input and
prepared output are each limited to 8 MiB, with at most 25 megapixels. Invalid,
unsupported, oversized, or unavailable images are skipped so a later valid
attachment can be used. URLs, replied-to images, and multi-image reasoning are
not supported. If several images are attached, the bot acknowledges the
one-image limit. Text and image mentions share capacity with
API inference: one active action and at most two interactive waiters, served
in FIFO order. Further requests receive a retry-later response; each user can
have only one pending mention. Attachment bytes are
downloaded only after a job is admitted, are never logged or persisted, and are
never included in memory consolidation. Vision jobs reserve more model context
for image tokens by limiting memory plus recent history to 4,000 characters
instead of the normal 6,000.

Bot and API coordinate through POSIX file locks in `LLM_CAPACITY_DIR`. The bot
reserves capacity before queuing a job and holds the same reservation across
retries and nested natural-command inference. Cancelling an async caller
retains execution capacity while its HTTP worker thread is still running.
Teases, ordinary reactions, inactivity nudges, birthday greetings, memory
synthesis, and optional price-change prose run only when shared capacity is
idle and take no interactive waiting positions. Birthday, memory, and inactivity
work remains eligible for its existing scheduler after a skip; deterministic
price notifications still continue without generated prose.

Coordination supports one macOS/Linux host with a shared local filesystem,
including the Compose `/data` volume. Both services must use the same lock
directory. Multi-host and network-filesystem coordination are outside scope.
Process exit releases kernel locks, but a killed or timed-out HTTP client cannot
prove that llama-server has stopped its remote computation.

Mention prompts tell the model who it is (the bot's server nickname) and who it
is replying to (the requester's display name), and ask it to start with the
answer. In chat history and replied-to messages, the bot's own lines are marked
`Name (you):`. Saved memory is labelled as notes about the requester, who is
addressed as `you` rather than having their details adopted as the assistant's
own. When a mention also @mentions other server members, up to two of their saved
profiles (1,000 characters each) are included as notes about them, so the bot can
answer questions such as "what do you know about @Alex?". Members who opted out,
channels without memory (automatic mode), and DMs are excluded. Validation removes leading requester/bot labels before comparing
the reply with the question, including short questions and differences in
case, punctuation, or diacritics. Echoes receive one corrective retry; a second
echo produces a generation-failure message instead of posting the question.
Replies that quote a question and then add an answer are accepted. These checks
detect textual repetition; they do not judge the correctness of an answer or
recognize every semantic paraphrase.

Mention answers and empty-ping summons also use a concise system instruction
that asks for a ready-to-send final answer without thinking, analysis, planning,
or preamble. It is intended to preserve the reply budget for visible output;
the existing token defaults and configuration remain unchanged. Teases,
reactions, inactivity messages, birthdays, memory extraction, scraper calls,
and other generators do not receive this instruction. Model and chat-template
behavior varies, so the instruction reduces unwanted thinking output but cannot
guarantee its suppression.

Every inference request has an output-token limit: 384 by default for mention
replies, 96 for short social replies, 32 for reactions, and 1,024 by default
for memory extraction. Configure mention replies with
`LLM_MENTION_MAX_TOKENS` and memory extraction with `LLM_MEMORY_MAX_TOKENS`.
The HTTP timeout limits the client's wait; it is not a server CPU limit.
Token-limit-truncated responses are treated as failures so incomplete memory
extractions cannot acknowledge and delete their source messages.

Logs include queue admission/rejection, job start/end, scheduler skip reasons,
memory batch and commit counts, HTTP lifecycle events, prompt/output sizes,
token usage, prompt-cache usage, and llama-server prompt/generation timings,
without message, prompt, completion, or image contents. Each inference gets a
short request ID for correlation, and every log line includes the logger name,
process ID, and thread name. A job-start line without a request-start line
points to preparation; a request-start line followed by a long HTTP wait points
to llama-server or the connection. CPU usage during model inference is
expected; these logs help distinguish slow inference from a bot stall. Set
`LOG_LEVEL=DEBUG` for per-observation memory capture metadata. The deployed
llama-server process must be inspected separately to confirm which process is
consuming CPU.

The bot may react to the original human message with a contextual emoji. It
does not seed feedback reactions on its own reply. The requester may manually
add 👍 or 👎 to that reply to rate the answer; reactions from the bot or any
other user are ignored. The database retains only reply
metadata, server, model alias, prompt version, final rating, and timestamps—not
prompts or response text. `/llm-feedback-summary` marks a model/prompt
combination ready to compare only after ten ratings; it never changes a model
or prompt automatically.

`LLM_MEMORY_ENABLED` selects one of two modes when the Discord process starts.
Only the exact value `1` enables automatic memory; `0`, a missing value, or an
invalid value selects manual memory. Restart the bot to change modes. Stored
entries are retained across mode changes, and the API/dashboard remain
available in both modes. `LLM_MEMORY_DISABLED=1` overrides both modes: memory
commands are not registered and mention prompts never include saved notes;
stored entries are kept for when it is turned back on.

In manual mode, `/memory-add` stores one user-authored memory categorized as
Likes, Dislikes, Facts, Interests, Opinions, or Other. `/memory-show` groups
the complete stored text by type, and `/memory-erase` deletes matching pasted
text or everything in the current scope when its argument is omitted. Matching
ignores case and repeated whitespace. Server memory is available in every
channel of that server; DMs use a separate scope. Previously synthesized
entries remain visible and are still supplied to mention prompts. Manual mode
does not capture messages, load pending observations for processing, or start
the synthesis scheduler.

In automatic mode, persistent memory stores cycle syntheses as individual
facts, impressions, likes, dislikes, topics, interests, opinions, and other
durable notes with stable IDs. Only an explicit newer
statement can correct a cited row. Exact duplicates are ignored, unrelated
rows are retained, and storage is not cut down to the prompt size. Existing
compact profiles migrate automatically on startup.

New user messages are temporarily stored as pending synthesis input. A new
cycle starts only when one enabled channel has at least 50 captured, permitted
user messages that are at least 600 seconds old. The newest eligible
observation becomes that cycle's fixed endpoint; messages arriving after it
wait for the next cycle. New-cycle scans run every five minutes by default and
are configurable with `LLM_MEMORY_CONSOLIDATION_INTERVAL_SECONDS`. An active
cycle drains one chunk at a time after the rest configured by
`LLM_MEMORY_ACTIVE_CHUNK_REST_SECONDS`, also five minutes by default.
After a successful chunk, the entries are saved and that chunk's source
messages are deleted in the same transaction. Failed synthesis retains the
chunk for retry so messages are not silently lost. Consecutive failures retry
after twice the active-chunk rest, then double up to the consolidation interval;
a successful commit resets this process-local backoff. Capacity skips preserve
the existing backoff and extraction progress and do not count as failures.
Each scan admits at most one
chunk of up to 10 messages / 3,000 source characters with up to 2,000
characters of retrieved entries. These are input-character budgets, not exact
token counts; JSON, escaping, instructions, and the model's chat template add
overhead. One extraction may return at most five additions and five corrections,
each limited to 200 characters. Remaining cycle observations are grouped by author, and the oldest
remaining author group is selected first. Scans reserve shared background
capacity before touching the database and skip when bot/API work is pending.
Successful memory work waits one active-chunk rest;
failed work follows the backoff above before another chunk is admitted. The
bot does not persist assistant replies or a raw conversation transcript. At
prompt time, relevant synthesized entries are ranked by word overlap and
recency and share the reference budget with live channel context. Memory
remains server-scoped (with a separate opt-in DM scope) and is never supplied
to another user.

---

## REST API

Data and configuration routes require `Authorization: Bearer <API_TOKEN>`. If `API_TOKEN` is missing, those routes return HTTP 503. Dashboard assets and `/health` are public. Base URL: `http://<HOST>:<PORT>` (from `.env`). In Postman, set that value as a Bearer Token at collection level and send JSON request bodies with `Content-Type: application/json`.

JSON bodies accept Discord IDs as integers or decimal strings. Browser clients
can send `X-Discord-ID-Format: string` to receive `guild_id`, `user_id`, and
`channel_id` fields as strings without JavaScript precision loss. Database row
IDs such as reminder, joke, item, and tracker IDs remain numeric.

### Dashboard and host

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | Local dashboard HTML; CSS and JavaScript are served below `/static/` |
| `GET` | `/health` | Public API/database probe: `{ "status": "ok", "database": "ok" }` with HTTP 200; unavailable database returns HTTP 503. |
| `GET` | `/system/stats` | CPU, temperature in Celsius, memory, disk, host uptime, platform, and server timezone; unavailable metrics are `null` |
### Sponsor tiers

| Method | Path | Body / notes |
|---|---|---|
| `GET` | `/sponsors/tiers` | Effective global catalog with `{ "id", "name", "price_per_year", "chance" }`; `chance` is a 0–1 probability and `price_per_year` is a decimal string in lei. |
| `POST` | `/sponsors/tiers` | Create a tier from exactly `{ "name", "price_per_year", "chance" }`; returns the saved stable ID with HTTP 201. |
| `PUT` | `/sponsors/tiers/<tier_id>` | Replace the three editable fields of an existing tier; returns HTTP 200. Unknown IDs return 404; duplicate normalized names return 409; malformed values return 400. |

These typed routes require the configured bearer token.
`PUT /settings/sponsor_tiers` is intentionally rejected so a generic setting
write cannot bypass tier validation. `GET /settings/sponsor_tiers` remains
available for diagnosis. A corrupt catalog or SQLite failure returns an
explicit server error rather than silently presenting or saving built-in
defaults.

Mutation requests reject non-object JSON and non-finite numbers. Generic settings cannot bypass the typed sponsor-tier or mention-model routes. Provider errors and application log output redact API keys and bearer credentials.

The API manages stored data and configuration. It does not post to Discord,
emulate Discord interactions, or run shell commands. `POST /wishlist/add` and
`POST /wishlist/refresh` fetch product pages to update saved tracking data;
the shared scraper can invoke local LLM extraction when ordinary extraction
fails. Refresh never sends a notification. Flight credential setup validates
the key with SerpApi. This API is an operator interface: `user_id` selects data
and does not authenticate a Discord user.

Startup isolates command sync and each background feature so one failure does not prevent other loops from starting. Frequent message/admission and tracking database operations run in worker threads to keep Discord responsive during SQLite lock waits. The notification loop contains transient database errors and retries on later ticks. Recovered recurring reminders clear terminal timestamps, and cleanup retains pending reminders.

SQLite initialization enables WAL before transactional schema migrations;
connections enforce foreign keys and use an explicit five-second lock wait.
Initialization failures stop bot startup. The API initializes lazily, returns
HTTP 503 on failure, and retries initialization on a later request. Keyword
inserts report success only after commit and invalidate their cache afterward;
failed writes return a Discord error or API HTTP 500.

### Keywords

| Method | Path | Body / notes |
|---|---|---|
| `POST` | `/keywords/add` | `{ "guild_id", "keyword", "response" }` — `200` after commit; `500` if saving fails |
| `DELETE` | `/keywords/delete` | `{ "guild_id", "keyword", "response"? }` — omit `response` to delete all for keyword in that guild |
| `GET` | `/keywords/get?guild_id=<id>` | Map of keyword → list of responses for one server |
| `GET` | `/keywords/top?guild_id=<id>&user_id=<id?>&limit=<1-100?>` | Usage counts; `user_id` optionally limits results to one requester |

### Reminders

| Method | Path | Body / notes |
|---|---|---|
| `POST` | `/reminders/add` | `{ "user_id", "channel_id", "remind_at", "message" }` — `remind_at` Unix timestamp |
| `DELETE` | `/reminders/delete/<id>` | |
| `GET` | `/reminders/all` | Array of reminder objects |

### Birthdays

| Method | Path | Body / notes |
|---|---|---|
| `GET` | `/birthdays` | All saved birthday registrations and destinations |
| `PUT` | `/birthdays/<user_id>` | `{ "channel_id", "guild_id"?, "month", "day" }`; omit or null `guild_id` for a DM destination |
| `DELETE` | `/birthdays/<user_id>` | Delete the selected user's birthday registration |

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

### LLM feedback and guild activity

| Method | Path | Body / notes |
|---|---|---|
| `GET` | `/llm/mention-model` | Active mention model and the environment-defined allow-list |
| `PUT` | `/llm/mention-model` | `{ "model" }` — accepts only a model in `LLAMA_CPP_ALLOWED_MODELS` |
| `GET` | `/llm/feedback/summary?guild_id=<id>` | Aggregated ratings by category, model, and prompt version; no prompt or response text |
| `GET` | `/inactivity/guilds/<guild_id>` | Whether automatic inactivity messages are enabled |
| `PUT` | `/inactivity/guilds/<guild_id>` | `{ "enabled": true }` |

### Persistent LLM memory

| Method | Path | Body / notes |
|---|---|---|
| `GET` | `/memory/channels?guild_id=<id>` | Enabled memory channels in one server |
| `GET` | `/memory/channels/<guild_id>/<channel_id>` | Memory status for one channel |
| `PUT` | `/memory/channels/<guild_id>/<channel_id>` | `{ "enabled": true }`; disabling also clears that channel's pending observations and cycle progress |
| `GET` | `/memory/users/<user_id>?scope_id=<id>` | Preference, synthesized entries, and any legacy profile; `transcript` remains an empty compatibility field; use scope `0` for DMs |
| `PUT` | `/memory/users/<user_id>/preference` | `{ "scope_id", "enabled" }`; opting out also erases saved memory |
| `DELETE` | `/memory/users/<user_id>` | `{ "scope_id" }`; forget memory without changing the preference |
| `DELETE` | `/memory/guilds/<guild_id>` | `{ "confirmation": "PURGE" }`; preserves channel settings and preferences |

The bot refreshes memory channel and preference caches periodically and checks
pending observation ownership before applying an in-flight memory update. API-side
privacy changes therefore take effect without restarting the Discord process.
Channel and preference controls govern automatic mode; the API remains available
in manual mode for inspecting, deleting, or preconfiguring retained data.

### Wishlist

| Method | Path | Body / notes |
|---|---|---|
| `POST` | `/wishlist/add` | `{ "user_id", "url" }` — **live scrape**; `201` with item fields; `400` invalid input, `409` duplicate, `422` unsupported page, `500` failed save, `502` blocked/unreachable, `503` temporarily busy |
| `DELETE` | `/wishlist/remove` | `{ "user_id", "url" }` |
| `GET` | `/wishlist/all` | All tracked items incl. `last_alert_kind`, `last_alert_price` |
| `GET` | `/wishlist/preferences?user_id=<id>&url=<url>` | One requester-owned item's current tracking data and preferences |
| `GET` | `/wishlist/history?user_id=<id>&url=<url>` | Item metadata plus chronological saved price observations; 404 when the item is not owned/found |
| `PUT` | `/wishlist/preferences` | `{ "user_id", "url", "target_price", "target_currency", "restock_only" }`; use `{ "clear_target": true }` to clear a threshold |
| `POST` | `/wishlist/refresh` | `{ "user_id", "url" }` — updates tracking data without notifications; `422` unsupported, `502` blocked/unreachable, `503` temporarily busy, `500` failed save; failed checks retain saved data/history |

Product fetches accept public HTTP(S) URLs only. Credentials in URLs, internal hostnames, private/reserved addresses, and redirects to them are rejected. DNS addresses are checked and pinned for each redirect hop, environment proxies are bypassed, downloads are limited to 5 MiB, and at most six requests follow a redirect chain.

Product HTTP fetches have a 15-second timeout; LLM fallback may additionally wait for capacity and generation under `LLAMA_CPP_TIMEOUT`. Add does not create rows for blocked, unsupported, or busy results.

### Flight tracker

| Method | Path | Body / notes |
|---|---|---|
| `GET` | `/flights/credentials?user_id=<id>` | Credential status and timestamp only — never returns the key |
| `POST` | `/flights/credentials` | `{ "user_id", "api_key" }` — validates the SerpApi key before storage |
| `DELETE` | `/flights/credentials` | `{ "user_id" }` |
| `GET` | `/flights/trackers?user_id=<id>` | All trackers belonging to one requester |
| `POST` | `/flights/trackers` | `{ "user_id", "origin", "destination", "start_date", "end_date", "adults"?, "currency"? }`; requires stored credentials and does not make an immediate paid search |
| `GET` | `/flights/trackers/<tracker_id>?user_id=<id>` | One requester-owned tracker |
| `DELETE` | `/flights/trackers/<tracker_id>` | `{ "user_id" }` |
| `GET` | `/flights/trackers/<tracker_id>/history?user_id=<id>` | Saved price observations for one requester-owned tracker |

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

**CI** — GitHub Actions runs `pytest` on every push/PR (`.github/workflows/test.yml`) and builds the Docker image plus validates `docker-compose.yml` (`.github/workflows/docker.yml`).

Coverage highlights: the `db` package (CRUD, migrations, cascades, memory, flights, and analytics), `flight_provider.py` (SerpApi key validation and Google Flights response parsing), `wishlist` parsing/currency/alert services, registered feature commands, row-level user memory, contextual reactions, and mention vision validation/single-slot admission/multimodal payloads.

Reliability checks use the repository `.venv` (Python 3.9.6) and cover shared
capacity across processes, cancellation, background skips, usable scraper
evidence, SQLite initialization retry/rollback, and truthful write failures.
The existing urllib3/LibreSSL compatibility warning remains. The reviewed bilingual
parser corpus passed 148/148 cases (100%) with zero unauthorized actions.
Tests use isolated databases; no live Discord messages or production services
were used.

On 2026-10-09, the integrated targeted run passed 601 tests and the full suite passed all 1,323 tests. New regressions cover public-only bounded scraping, transactional writes, auth/health/redaction, reminder recovery, durable flight alerts, quota selection, and isolated startup. Diff and Python syntax checks passed. Docker validation was unavailable locally because Docker is not installed.

Tests use an isolated DB per case (`tests/conftest.py`); your live `responses.db` is never touched.

---

## Project layout

### Root

| File | Role |
|---|---|
| `bot.py` | Discord client, feature wiring, `on_message` / `on_ready` |
| `api.py` | Small Flask process entry point; application and routes live in `web/` |
| `templates/dashboard.html`, `static/dashboard.*` | Build-free local administration dashboard |
| `sponsor_tiers.py` | Shared sponsor-tier validation, versioned catalog codec, decimal/percent formatting |
| `wishlist/` | Scraping, refresh persistence, currency conversion, alerts, and chart rendering |
| `flight_provider.py` | SerpApi Account/Google Flights client and IATA/date validation — **no** Discord imports |
| `assistant_profiles.py` | Discord-free assistant-profile validation, defaults, and prompt guidance |
| `natural_commands.py` | Discord-free bounded English/Romanian command parser |
| `db/` | SQLite connection, schema/migrations, and domain query modules |
| `db/sponsors.py` | Atomic sponsor-tier catalog reads/writes using the global settings table |
| `llm/` | llama.cpp client, response generation, memory extraction/store, and single-slot worker |
| `web/` | Flask application factory, authentication/helpers, and four route blueprints |
| `logger.py` | Rotating logs (5 MB × 2); optional `LOG_DIR` location and `LOG_LEVEL` verbosity |
| `Dockerfile`, `docker-compose.yml` | Docker image and bot + API services |
| `responses.db` | Runtime DB (gitignored); path overridable via `DB_FILE` |

### `features/`

| Module | Class | Role |
|---|---|---|
| `response_gate.py` | `ResponseGate` | Cooldown between keyword replies and teases |
| `keywords.py` | `KeywordsFeature` | Per-guild keyword match, `/keyword-add`, `/top-keywords` |
| `teases.py` | `TeasesFeature` | Mood teases (LLM-enhanced), `/mood` |
| `inactivity.py` | `InactivityFeature` | Guild activity tracking, inactivity nudges |
| `reminders.py` | `RemindersFeature` | `/remind`, delivery loop |
| `jokes.py` | `JokesFeature` | Joke pool + per-guild schedule commands and loop |
| `sponsors.py` | `SponsorsFeature` | Sponsor state, `/sponsor-set`, `/sponsor-set-tiers`, plan display, and expiry |
| `wishlist.py` | `WishlistFeature` | `/wishlist-*` commands, scrape loop, and Discord notifications |
| `wishlist_graphs.py` | `WishlistGraphView`, `CustomDaysModal` | Saved-history filtering, graph buttons, custom periods, and percentage comparison |
| `flights.py` | `FlightTrackerFeature` | `/flight-tracker-*` login and tracker commands, immediate searches, five-hour checks, lower-price DMs |
| `assistant_profiles.py` | `AssistantProfilesFeature` | Private global per-user preference commands |
| `natural_commands.py` | `NaturalCommandsFeature` | Mention-only immediate actions and private requester delivery |
| `stats.py` | `StatsFeature` | `/stats` |
| `llm_mention.py` | `LLMMentionFeature`, `ContextReactionFeature` | Prioritized @bot replies, contextual reactions, and background memory work through llama.cpp |
| `llm_feedback.py` | `LLMFeedbackFeature` | Requester-only 👍/👎 ratings for generated mention replies |
| `user_memory.py` | `UserMemoryFeature` | Discord commands and event adapter for the shared memory store |
| `mention_utils.py` | — | Parse @bot mentions using `BOT_ID` |
| `onboarding.py` | `OnboardingFeature`, `OnboardingView`, `HelpView` | Guided private `/start`, timezone selectors, and feature navigation |
| `natural_language_help.py` | `NaturalLanguageHelpView` | Reusable read-only bilingual mention-command examples |
| `help_feature.py` | `HelpFeature` | `/help` and `/natural-language` |
