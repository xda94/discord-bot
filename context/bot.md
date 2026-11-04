# Discord Bot Architecture Notes

This living document summarizes the repository structure, runtime behavior,
data model, external integrations, testing status, and notable implementation
details. It was created from a codebase review on 2026-09-14 and updated through
2026-10-12, including vision retries, diagnostics, and the reported Gemma 4
deployment.

## Project overview

The project is a Python Discord bot accompanied by a Flask administration API. The two processes use the same SQLite database and can run either through Docker Compose or as separate PM2 processes.

The major capabilities are:

- Per-server keyword responses and keyword usage statistics
- Mood-based random teases
- Queued local-LLM text and image responses when the bot is mentioned
- One-off and recurring reminders with lifecycle records, retry/reconciliation,
  snooze, and private management controls
- Global per-user assistant profiles for language, tone, currency, explicit
  timezone confirmation, notification style, quiet hours, and digest delivery
- Per-user recurring birthdays with LLM-generated greetings
- Per-server scheduled daily jokes
- Sponsor tags appended to some keyword responses
- Wishlist price and stock tracking, target-price/restock alerts, manual refreshes,
  price graphs, currency conversion, and buy/wait alerts
- Per-user fixed-date flight price tracking through SerpApi, optional budget
  crossings, and checked-in airport/city autocomplete
- Inactivity nudges
- Host system statistics
- Romanian Orthodox calendar lookup through azisespala.ro
- Private `/worth-it` buy-or-wait commentary for tracked wishlist items
- Deployer-only daily usage analytics for commands, mentions, controls,
  automatic behavior, scheduled deliveries, background processing,
  per-user wishlist/flight usage, and delivery backlog state

## Runtime architecture

There are two executable Python entry points.

### `bot.py`

`bot.py` owns the Discord process. During module startup it:

1. Loads `.env` values.
2. Configures logging before importing feature modules.
3. Validates the llama.cpp model-alias configuration.
4. Requires `BOT_ID` and `DISCORD_TOKEN`.
5. Enables Discord's message-content intent.
6. Creates one `discord.Client` and one `app_commands.CommandTree`.
7. Initializes the SQLite schema and migrations.
8. Constructs every feature object.
9. Runs the Discord client.

When Discord signals `on_ready`, command sync, catalog refresh, and each background feature start independently; one failure is logged without preventing the others. Repeated ready events are safe because each feature checks whether its loop is already running. Frequent message/admission writes and reminder/notification/flight/wishlist scheduling operations use `asyncio.to_thread` to avoid blocking Discord during database lock waits.

### `api.py`

`api.py` is the executable wrapper for a separate threaded Flask process.
`web/app.py` creates the application with domain blueprints. The process:

- Requires `HOST` and a numeric `PORT` at import/startup time.
- Requires `API_TOKEN` bearer authentication for all protected data/configuration routes.
- Returns HTTP 503 for protected routes when the token is absent; dashboard/static assets and `/health` remain public.
- Initializes the database lazily before the first request with a lock around initialization.
- Uses the same `DB_FILE` as the Discord process.
- Serves a responsive HTML/CSS/JavaScript dashboard at `/` for LAN
  administration, host metrics, and saved-data analytics.

The API covers keyword CRUD/analytics, reminders, joke pools and guild schedules,
wishlist snapshots/preferences/refresh, mention-model configuration, guild inactivity switches, flight credentials/trackers/history, and
global settings. A public `/health` probe checks API/database availability and
returns HTTP 200 or 503 without credentials or exception details. It is an operator
data/config interface: request `user_id` fields select records; they do not
authenticate Discord users. No Discord posting or interaction emulation is
exposed. Wishlist add/refresh fetch product pages through the shared scraper,
whose extraction fallback may call llama.cpp. Credential setup validates keys
with SerpApi; adding an API flight tracker waits for the bot's normal checking
cadence instead of searching immediately.

### Deployment model

`docker-compose.yml` runs two containers from the same image:

- `bot`: runs `python bot.py`
- `api`: runs `python api.py` and publishes the configured port

Both mount the same `bot-data` volume. Inside Docker, the database is `/data/responses.db` and logs are written below `/data/logs`.

The documented non-Docker deployment uses two PM2 processes. `package.json` is currently empty; Node is only relevant because PM2 is used as the process manager.

The reported production hardware is an Intel N150 with 16 GB RAM. It is not a
Pi Zero W. The llama.cpp service currently has a 10 GB systemd memory limit and
a 32,768-token context.
Docker uses Python 3.13, `vl-convert-python`, and DejaVu fonts. Wishlist PNGs
render locally without Chrome, Altair, or Matplotlib. Install changed Python
requirements and rebuild the image when deploying renderer changes.

## Discord event flow

Every non-bot message passes through the handlers in this order:

```text
InactivityFeature
    -> LLMMentionFeature
    -> KeywordsFeature
    -> ContextReactionFeature
    -> TeasesFeature
```

The semantics are:

- `InactivityFeature` records guild activity and always allows dispatch to continue.
- `LLMMentionFeature` returns `True` for a bot mention, including cooldown replies, so keyword and tease handling stop.
- `KeywordsFeature` returns `True` after sending a keyword response, so teasing stops for that message.
- `ContextReactionFeature` occasionally queues a contextual emoji for ordinary
  messages while the model worker is idle; a selected message skips teasing.
- `TeasesFeature` is last and always returns `False` after its optional response.

Direct messages are eligible for LLM mention handling and random teases.
Buttons and modals identify their action without a mention. Keyword matching
requires a guild.

Public slash-command names use hyphens: `/keyword-add`, `/top-keywords`,
`/joke-*`, `/sponsor-*`, `/llm-*`, and `/flight-tracker-*`. Flight options are
`start-date`, `end-date`, and `tracker-id` via Discord option renaming; internal
Python names and API JSON fields retain underscores. Startup `tree.sync()`
publishes the renamed commands. `tests/test_command_naming.py` guards the naming
convention.

## Feature modules

### `assistant_profiles.py` and `db/assistant_profiles.py`

Store one global preference row per Discord user. `/start`
writes language and timezone; no command or dashboard control changes the other
stored values, which are still honored. Defaults are `auto` language, `default` tone, no currency
override, `UTC`, immediate delivery, no quiet hours, `standard` notifications,
and `balanced` LLM behavior. Partial updates preserve omitted fields, reads do
not create rows, currency is limited to the existing five supported currencies,
and timezones are validated with `zoneinfo.ZoneInfo`.

Text, vision, and empty-summon jobs receive an immutable profile snapshot when
queued. Static output resolves language in this order: an explicit request,
saved preference, detected request language, then English. Scheduled output
uses the saved language or the language captured at creation. Saved currency is
used only where wishlist and flight currency options are omitted. Calendar
times, recurrence, quiet hours, and daily digests require an explicitly
configured timezone; relative one-off reminders do not.

### `features/keywords.py`

Provides:

- `/keyword-add`
- `/top-keywords [user]`
- Automatic keyword responses

Keyword maps and statistics are scoped by Discord guild. Matching is case-insensitive and uses word-boundary-style regular expressions, so a keyword does not normally match inside a larger word.

Multiple responses can exist for one keyword. The feature prefers a response different from the last one used for that guild/keyword pair, without looping indefinitely when duplicate response strings exist.

After a successful response it:

- Optionally appends a sponsor suffix.
- Updates the response cooldown.
- Records keyword usage in SQLite.

### `features/response_gate.py`

Contains a small in-memory time gate with a default ten-second cooldown.

Although comments in `bot.py` and the class documentation describe it as shared by keywords and teases, only `KeywordsFeature` currently receives and checks the gate. `TeasesFeature` does not use it.

### `features/sponsors.py`

Provides:

- `/sponsor-set`
- `/sponsor-plans`
- `/sponsor-who`

Sponsor configuration is stored in the global settings table. Sponsor setup is protected by a password modal. A configured plan controls the probability that a sponsor message is appended to a keyword response. `/sponsor-plans` is generated from the canonical `SPONSOR_TIERS` mapping, so its displayed names, annual prices, and append probabilities cannot drift from the values used by the feature. An hourly task warns about expiry and clears expired sponsorships after one year.

Because sponsor state is global, it applies across all guilds served by the bot.

### `features/llm_mention.py`

Provides:

- Replies when the configured bot ID is mentioned
- Inspects the first processable directly attached JPEG, PNG, GIF, WebP, BMP, or TIFF on a mention
- `/llm-set`

All bot and API inference shares one active action and at most two interactive
waiters, served in FIFO order through POSIX file locks. The bot reserves globally
before adding a mention to its persistent worker queue; a fourth action receives
a retry-later response. Reservations span retries. Cancelled callers retain execution capacity until any running HTTP
worker thread finishes. Per-user pending protection and cooldowns still apply.
Ordinary-message reactions start only when shared capacity is idle and occupy
no interactive waiting positions.
llama.cpp calls run in worker threads, preventing blocking inference from
freezing the Discord event loop. Each user may have only one pending mention
job. `llm/client.py` sends requests to llama-server's
OpenAI-compatible `/v1/chat/completions` endpoint and defaults to a user-only
message. Mention answers and summons opt into a short system instruction that
asks for direct final output without thinking blocks; other generators retain
the user-only default.

The compact user prompt puts stable response rules before recent
`<chat_history>` and the dynamic `<current_message>`, allowing llama-server to
reuse a longer common prompt prefix. Configured history resolves short
contextual questions such as “pareri?” or “what do you think?”. The model must
produce exactly one ready-to-send reply in the current message's language and
must produce a new answer or reaction rather than repeating or merely
paraphrasing the current message. The prompt explicitly asks it to answer from
the assistant's perspective and start with the answer. Drafts, translations, and
coaching are permitted when requested. Chat history
and a directly replied-to message help resolve meaning; a replied-to message
never selects the response language. Structured mention output contains answer text plus an
optional allowed emoji. Invalid JSON, empty text, and obvious echoes receive one
retry before the bot returns a short generation-failure response.
Echo validation runs after shared requester/bot label cleanup, handles short
questions, and ignores differences in case, punctuation, and diacritics.
This rejects both `Esti okay?` repeated verbatim and `De ce te comporti urat cu
Schular?` repeated with restored Romanian diacritics. The retry includes the
validation reason and directs the model to answer, rather than copy or correct
the question. A quoted question followed by an actual answer remains valid.
This is a textual repetition check, not a semantic correctness check.
Validation warnings include the model alias and specific rejection reason
(such as empty text, an echo, or an unsupported reaction), without logging
prompt or completion text. Text that becomes empty after normalization is
reported separately from an echo.

Each request includes a finite `max_tokens` value. Mention replies use 384 by
default, configurable through `LLM_MENTION_MAX_TOKENS`; short teases, summons,
inactivity nudges, and price messages use 96; and reactions use 32. The
configurable limit has a minimum of 1. This
avoids llama-server's unbounded generation default. A response ended because
it exhausted its token limit is rejected, even if its partial text is valid
JSON.

The feature tracks:

- A per-user pending-job set, allowing only one active request per user
- A per-user cooldown measured after a job finishes
- Optional recent channel context, controlled by `LLM_CONTEXT_MESSAGES`
- A global model selection stored as `settings.mention_model`

Long model output is split into Discord-safe message chunks. A bare mention uses a separate summon-response prompt. A bare mention with an image is instead a vision request that asks for a concise description. A captioned image answers or solves exactly what the caption requests, with visual claims grounded in the attachment.
The sender removes leading requester/bot mentions and labels, including
Markdown-wrapped forms such as `@Robeeque Balen:`, then inserts one real
`<@requester_id>` mention into the first chunk. Names used naturally later in
the answer are preserved. Only the
requester is allowed to be pinged; role/everyone pings and reply-author pings
are disabled, and continuation chunks allow no mentions. Both normal and bare
mention replies use this path. The mention is produced in code rather than
depending on model output.
The bot may react to the original human message.

Inference capacity is shared across bot/API processes using the same lock
directory. The selected model is global rather than guild- or user-specific.

Ordinary chat has a default 10% chance of being considered for a reaction, with
a shared 60-second per-channel cooldown. Keyword responses retain priority, and
reaction inference is admitted only while shared capacity is idle. The model may
choose one emoji from a small Unicode allow-list or no reaction. Discord
permission/deletion failures are logged concisely without interrupting other
handlers. Missing `Add Reactions` permission prevents only the emoji reaction;
the mention reply still succeeds.

Mention replies follow the language of the current message, even when the
requester saved a different language in `/start`. The saved language applies
only when that message is too short to tell or is an uncaptioned image; with no
saved language, a too-short message falls back to channel history. Generated teases likewise derive their
language only from the triggering message; English prompt rules, mood names,
and style descriptions must not select the output language.

Vision accepts JPEG, PNG, GIF, WebP, BMP, and TIFF directly attached to the
mention, using the first image it can process in attachment order. Animated
images and multipage TIFFs use only the first frame or page. URLs, replied-to
images, and multi-image reasoning are not supported. Extra attached images
produce one acknowledgement of the one-image limit. Known input sizes above
8 MiB or dimensions above 25 megapixels are skipped before admission; missing
dimensions are validated from the downloaded image. Downloads begin only after
the job becomes active and vision is confirmed through `/props`
`modalities.vision: true`. Actual bytes determine the format, regardless of
filename or MIME metadata, and the first frame or page must decode fully.
Static JPEG/PNG bytes within `LLM_IMAGE_MAX_EDGE` (default 1280 pixels) are
preserved. Larger images are resized proportionally with EXIF orientation applied;
resized JPEGs stay JPEG, and other formats become RGB/RGBA PNG. Transparency is
preserved. Logs include source/prepared dimensions and byte counts. Both input
and prepared output must fit the 8 MiB
limit, and decoded dimensions must fit the 25-megapixel limit. Download,
decode, unsupported-format, or limit failures advance to the next candidate;
processing stops after one succeeds. Bytes exist only for that active request
and are neither persisted nor logged. The worker returns a specific message for
disabled/unavailable vision, failed image processing, or inference failure.
Vision prompts use a 4,000-character history budget.

Production logs from 2026-10-12 confirmed an image-bearing HTTP request timing
out after 180 seconds despite advertised vision support. Image requests now
use `LLAMA_CPP_VISION_TIMEOUT`, defaulting to the larger of 600 seconds and
`LLAMA_CPP_TIMEOUT`; explicit per-call timeouts still win. `LlamaCppTimeoutError`
preserves the wait limit through mention generation to a localized worker reply.
HTTP timeouts are not retried automatically, since server inference may continue.
These changes still require validation against the deployed CPU server.

Generic English/Romanian image-access denials trigger one retry with the same
image and current question, without stale chat. Repeated denials
fail through the normal processing-error path; uncertainty about specific visual
details remains valid. The retry preserves the requester's assistant profile.
`scripts/check_vision.py` compares a simple direct request with the
structured bot prompt using a generated red circle, without Discord traffic.

### `features/teases.py`

Provides `/mood` and optional random replies to ordinary messages.

The base probability is 10%, divided by `1 + teases_today`, so teasing becomes progressively less likely during the day. Its counter resets on the local calendar date. Tease templates can be rewritten through llama.cpp, with fallback behavior implemented in `llm/responses.py`.

Mood and daily tease state are in memory and reset when the process restarts.

### `features/inactivity.py`

Tracks the latest guild message in memory and in the `guild_activity` table. Every 30 minutes it checks for guilds with at least 24 hours of inactivity. Members with **Manage Server** or Administrator permission can use `/llm-inactivity activate` or `/llm-inactivity deactivate` to control the feature independently in each server; unconfigured servers default to enabled. Disabled servers keep recording activity but are skipped before any channel lookup or llama.cpp request.

When nudging a channel, it tries to select a random human from the latest 50 messages. Every nudge is generated by llama.cpp without a fixed topic or message pool. If generation fails, nothing is posted and the overdue guild is retried during the next scheduled check.

Every guild message writes an activity UPSERT through `asyncio.to_thread`, keeping SQLite lock waits off the Discord event loop.

### `features/reminders.py`

Provides `/remind <when> <who> <what> [recurrence]`, `/reminder-list`,
`/reminder-edit`, `/reminder-cancel`, and `/reminder-snooze`. Recurrence supports daily,
weekdays, and weekly local wall-clock schedules.

Reminders persist creator, recipient, timezone, captured language, recurrence,
state, attempts, next retry, failure details, revision, and Discord message
identity. States are `pending`, `sending`, `delivered`, `failed`, and
`cancelled`; terminal rows and occurrence history are retained for 30 days.
Due occurrences are claimed transactionally. Transient failures retry after
one, five, and fifteen minutes; permission failures remain visible as failed
records. An uncertain send is reconciled from the persisted occurrence marker
before any explicit retry. On restart, a missed one-off is delivered once and
missed recurring occurrences coalesce before the next future local occurrence.

Edit and cancel operations are owner-checked for either creator or recipient.
Snooze creates a separate ten-minute one-off and never shifts a recurring
schedule. Reminder delivery is timely and bypasses tracking quiet hours and
daily digests. Reminder lists and delivered messages expose only the controls
valid for their lifecycle state.

The reminder loop runs every ten seconds, sends to the original channel, and
keeps the durable record after delivery or failure. Success reactions and
private list results remain reaction-only/private as described above.

Recovered recurring reminders return to pending with `finished_at` cleared. Cleanup deletes only terminal reminders, retaining pending legacy rows even if they carry a stale terminal timestamp.

### `notifications.py` and `db/notifications.py`

Wishlist and flight alerts enter a durable SQLite outbox before any Discord
send. Pending events coalesce by user, source, item, and event kind while
target and restock events remain distinct. The outbox stores the captured
language, observed timestamp, protected user content, due time, retry state,
and accepted message-part progress so restarts and partial DM failures do not
silently discard alerts.

Immediate delivery is the default. Optional quiet hours and daily digests
require an explicitly configured profile timezone. Digest text identifies when
the price was observed and never implies that a fresh provider check occurred.
Reminder delivery bypasses these tracking notification controls. Failed and
pending counts are exposed through the dashboard and notification API.

### `features/birthdays.py`

Provides `/set-birthday [date]`. Dates use day-first `DD.MM`, `DD-MM`, or
`DD/MM` syntax with one- or two-digit components; years, natural-language
dates, blank supplied values, and impossible calendar dates are rejected.
Omitting the optional date deletes the saved registration. Each Discord user
has one global registration across the bot, so saving in another channel or
DM replaces both the date and destination. Successful saves and deletions are
confirmed privately.

The feature makes its first attempt at 00:01 using the bot process's local
calendar date. A registration receives one LLM-generated delivery per calendar year, with its successful
delivery year preserved when the date or destination is updated. February 29
is delivered only on leap years. Missing destinations, unavailable send
permissions, failed LLM generation, and Discord send failures leave the
registration due for retry every 30 minutes during that date; no successful
greeting is repeated and no belated greeting is sent.
Revision and date/year guards are repeated after generation to suppress stale
work when a registration changes during delivery. Blocking LLM generation
runs through `asyncio.to_thread`; greetings use no name, age, history,
or user-authored text. Scheduled delivery, failure, and state-write outcomes
are recorded in analytics using the saved guild or DM scope.

Sending and marking the SQLite state are not atomic, so a crash between them
can produce a duplicate and an already submitted Discord message cannot be
recalled. A single bot process is assumed; deleting and recreating a
registration starts a fresh revision and does not retain prior delivery
history. A registration made near midnight can miss that year's date, and an
unavailable bot or LLM cannot deliver it later.

### `features/jokes.py`

Provides:

- `/joke-add`
- `/joke-activation`
- `/joke-deactivation`
- `/joke-status`

The joke text pool is global. Schedule, destination channel, last-send date, and sent-joke history are per guild.

A 30-second loop evaluates every configured guild. Each guild receives every joke once before its sent history is reset and the pool starts again. Sending history is independent across guilds.

The feature includes a one-time migration from the previous global joke schedule stored in `settings` to `guild_joke_config`.

### `features/wishlist.py`

Provides:

- `/wishlist-item`
- `/wishlist-item-delete`
- `/wishlist-show`
- `/wishlist-graph <url> [currency] [days]`
- `/wishlist-graph-all [currency] [days] [percentage]`
- `/wishlist-target-price`
- `/wishlist-target-clear`
- `/wishlist-restock-only`
- `/wishlist-refresh [url]`

This module owns Discord commands, scheduled checks, cooldowns, and notifications.
The `wishlist` package contains the shared scraper, refresh persistence, currency,
alert classification, and chart rendering used by the bot and API.

Private lists include IDs and
owner-checked edit/cancel/restock controls. Target alerts fire at or below the
configured price and re-arm only after the price rises above it. Tracking
notifications are queued in a durable outbox, can be coalesced by item/event,
and preserve target/restock events while respecting the profile's immediate or
daily delivery mode and quiet hours. Every notification with a known current
price shows it once; buy alerts add the previous low and wait alerts the usual
median, in standard and compact styles. Wishlist and flight notifications end
with `Observed at DD.MM.YYYY HH:MM TZ` in the profile timezone (UTC default).

Wishlist fetches accept only public HTTP(S) destinations: URL credentials, local/internal hostnames, non-public DNS addresses, and redirects to them are rejected. Connections pin validated DNS addresses and bypass environment proxies. Product bodies are limited to 5 MiB and redirects to six checked requests. Successful refresh snapshots, freshness/status, and history commit together; save failures produce `database-error` rather than success. Target/restock preference updates validate before one atomic write.

The wishlist scraper runs every 12 hours. For each item it:

1. Fetches and parses the current page in a worker thread.
2. Compares price and stock with the last snapshot.
3. Adds a price-history point when available.
4. Evaluates low/high alert state.
5. Sends a DM for price changes, stock recovery, actionable price zones, or a
   target-price crossing (unless the item is restock-only).
6. Updates the stored item snapshot, alert state, and check freshness/status.

Items are processed sequentially with a politeness delay. Price history older than `WISHLIST_HISTORY_DAYS` (default 180) is deleted after a pass.
Target prices are stored with their requested currency and fire only once per
below-target period; they re-arm after the price rises above the target.
Restock-only items continue collecting snapshots and price history but only DM
on a confirmed out-of-stock to in-stock transition. A user can manually refresh
one owned item by supplying its URL, or refresh all owned items by omitting the
URL. Each item has its own five-minute cooldown, so cooling items are skipped
during an all-item refresh. The command reports source domain, current snapshot,
target progress, and last-check state without emitting a separate DM.
Manual refresh timestamps are held in process memory and reset on restart.
The HTTP `/wishlist/refresh` still requires one URL and does not share the
Discord cooldown. Unexpected per-item errors do not abort the bulk Discord
refresh, and replies are split to respect Discord's message length limit.

Price alerts require at least seven prior numeric points and meaningful price variance:

- Low: current price is at or below the historical minimum.
- High: current price is above the historical median.
- Repeated low alerts require at least a further 1% drop.
- High alerts re-arm after returning to the neutral/low range.

Every price increase or decrease adds one llama.cpp-generated reaction below
the fixed old/new price line. The generator receives the product name, price
direction, displayed prices, and percentage change, then randomly selects a
funny, mock-corporate, playful, serious, enthusiastic, or melodramatically sad
tone. It must not restate or invent price facts. LLM commentary is optional:
if inference fails, the factual Discord DM still goes out. Low/high alert
sections are factual and do not make an additional LLM request. The feature
imports `generate_price_change_message` from `llm/responses.py`; the function,
its tone map, and its compact data-grounded prompt must be deployed together
with `features/wishlist.py`.

A second loop refreshes EUR-based exchange rates every 24 hours for RON, DKK, EUR, USD, and GBP. Rate updates validate finite positive values and save the full set atomically; conversion uses a cached rate snapshot with a 60-second lifetime. Graphs use self-contained Vega-Lite specifications rendered locally by `vl-convert-python` and are sent as PNG Discord attachments. No browser or external chart service is involved.

### `features/wishlist_graphs.py` and `wishlist/charts.py`

The graph UI and data selection live in `features/wishlist_graphs.py`; renderer
specifications and PNG generation live in `wishlist/charts.py`.

- Both graph commands accept `days` from 1 to `WISHLIST_HISTORY_DAYS`, which is
  also the default (180 unless configured). Saved timestamps are filtered to the rolling selected period before conversion or comparison.
- Replies have 7/30/90-day buttons plus a Custom days modal accepting any whole
  number in that range. Controls edit the original private graph attachment.
- `/wishlist-graph-all` draws a two-column grid of mini charts, biggest drops
  first, each with its own price scale. `percentage:true` and its comparison
  button switch to a ranked board: each row shows the period's price range and
  today's price relative to the median `(price / median - 1)`, plus the change
  since the first observation. Products with a nonpositive starting or median
  price are skipped and reported. The board works without exchange rates, using
  each series' own observations. Both views show at most
  `MAX_COMBINED_PRODUCTS` (12), and the reply says when products were left out.
- Currency mode defaults to the single item's native currency or the majority
  currency for the combined graph; the user can override it. A single-product
  graph shades a green buy zone below its target when conversion is available.
- Identical or truncated titles get a ` #n` suffix so products stay separate.
  Lines are drawn as steps, without smoothing, because a price holds until a
  shop changes it.
- Time axes use UTC; labels include hours/minutes for short spans and years
  when a range crosses a year. Single-item charts lead with the current price,
  its distance from the median, and the change since the first observation.
- Data reads and rendering execute in a worker thread. Buttons read saved
  history; they do not refresh product pages. Missing history keeps controls
  available so the requester can choose another period.
- Controls are requester-only, expire after ten minutes idle, and do not
  survive a bot restart. A per-view lock prevents overlapping redraws; rendering
  errors retain the prior view and allow retry. They cannot restore observations
  deleted by the `WISHLIST_HISTORY_DAYS` retention policy.

### `wishlist/scraper.py`

Contains pure scraping primitives shared by the bot and API.

The extraction sequence is broadly:

```text
HTTP request
    -> Schema.org JSON-LD product/offers
    -> OpenGraph/product metadata
    -> visible page-text stock signals
    -> local llama.cpp JSON extraction fallback
```

Scripts and styles are removed before visible-text stock extraction, avoiding false signals from embedded localization or JavaScript data.

`curl_cffi` is used when installed to improve compatibility with sites that inspect TLS/browser fingerprints. The implementation falls back to ordinary `requests` when it is unavailable.

Usable evidence requires a finite nonnegative price, including zero, or explicit
boolean stock status. Titles and currencies alone cannot make a scrape succeed.
Boolean, negative, NaN, and infinite prices are rejected; blank metadata becomes
missing. Unknown JSON-LD/meta availability remains `None`, and recognized
out-of-stock values remain `False`. Fallback runs when price and stock are both
unknown, fills only missing fields, and precedes TLD currency guessing.

Failures distinguish blocked/unreachable, unsupported, and temporarily busy
results. Both Discord and API validate a live scrape before adding an entry.
Manual add/refresh fallback uses interactive capacity: Discord explains retry,
and the API returns HTTP 503 when capacity is unavailable. Scheduled fallback
uses background admission and skips under contention. Failed refreshes retain
saved snapshots, alert state, and history while recording the check outcome.

### `features/flights.py`

Provides:

- `/flight-tracker-add`
- `/flight-tracker-show`
- `/flight-tracker-delete`
- `/flight-tracker-login`
- `/flight-tracker-logout`
- `/flight-tracker-budget`

Each Discord user supplies their own SerpApi key through a private modal. Credentials are validated before being stored, and HTTP provider sessions are cached per user until credentials change.

New trackers use exact outbound and return dates. An initial search runs immediately. The scheduled loop runs at a configurable interval, five hours by default, and selects at most the least-recently-checked tracker for each user per pass to limit account quota use.

Departed and retired flexible trackers are excluded before selecting one tracker per user, so they cannot displace an active watch. The loop skips recent checks after restart and no longer queries departed trips. It sends a DM for the first successful price or a strict price decrease; equal or higher results remain silent.

Trackers may have an optional budget in their existing currency. A budget alert
fires on the first observed price at or below the threshold and re-arms only
after a later observation rises above it. Airport and city inputs use the
checked-in attributed catalog in `data/AIRPORTS.md` with Romanian aliases;
cities mapping to multiple airports require an explicit selection. Fixed dates,
cadence, provider quotas, and tracker ownership remain unchanged.

Flight observations, history, budget crossing state, and new outbox alerts commit in one transaction. Enqueue failures roll back the observation, preserving the old comparison price for retry. Temporary database failures cannot terminate the notification delivery loop. Database work for scheduling and delivery is offloaded from the Discord event loop.

Legacy Amadeus credentials are removed during database migration. Old flexible-date trackers remain visible but are excluded from scheduled selection and do not consume quota.

### `flight_provider.py`

Provider errors redact API keys, including encoded keys and request URLs, before being stored or returned; logging also redacts credential-shaped fields and configured secrets. Keys remain plaintext in SQLite.

Wraps SerpApi's account-validation and Google Flights endpoints. It normalizes and validates IATA codes, classifies provider/quota errors, parses the returned flight groups, and chooses the cheapest offer.

### `features/stats.py`

Provides host and process statistics through `/stats`, using `psutil`. It gathers CPU, memory, disk, temperature where available, platform, process uptime, and bot latency. Blocking collection runs outside the Discord event loop.

### `features/azi_se_spala.py`

Provides a Romanian calendar lookup backed by annual JSON files from azisespala.ro. Calendar data is cached in memory by year, fetched in a worker thread, and attributed in replies as required by the source.

### `features/humor.py` and `/worth-it`

`HumorFeature` registers only `/worth-it url:https://shop.example/item`, without
background tasks. It works in guilds and DMs. Guild invocations defer
ephemerally and deliver the full result as an ephemeral followup in the command
channel, visible only to the requester with Discord's Dismiss message control.
It never sends a DM; blocked DMs do not affect it. The completed result
prefixes the requester's mention and allows mentions only for that user.
`/worth-it` only reads
requester-owned `get_scraped_item` and `get_price_history`; no scrape, refresh,
purchase or history mutation. Current/minimum/maximum/median prices and signed
differences from median/minimum render outside model prose. At least seven valid
positive historical observations, a fresh known check, known currency and stock
are required for a favorable verdict. Below median or at the minimum means buy;
above median means wait; median or less than 1% range means neutral. Missing
facts, stale/unknown checks or stock and out-of-stock items produce distinct
non-buy outcomes. Comparisons assume the tracked item's native currency stayed
the same; historical currency changes cannot be detected from the existing
table. Both voices' action text is deterministic for the verdict. The LLM must
echo its decision and may add only bounded numeric-free, advice-free humor;
contradictory/invalid output falls back to the deterministic voices.

`features/humor_common.py` owns bilingual text and safe catalogs. `llm/humor.py`
uses existing shared llama.cpp capacity through `asyncio.to_thread`, one call
with at most 220 tokens and a 30-second timeout. Empty,
malformed, oversized, busy or timed-out output falls back locally with no
repair/retry call.

`db/community_schema.py` drops the legacy community tables with
`DROP TABLE IF EXISTS` on every `init_db`.

### `features/help_feature.py`

Provides `/help`. The help text is split into chunks below Discord's
2,000-character limit and lists `/worth-it`. `HelpView`
offers Reminders, Wishlist (including `/worth-it`), and Flights
categories plus a complete reference, and respects Discord's five buttons per
row/five rows, including the onboarding return control on row 4. Complete
reference renders slash-command descriptions and parameters.

### `mention_utils.py` and the `llm` package

- `mention_utils.py` recognizes the configured bot mention, removes it from prompts, and resolves the bot's guild display name.
- `llm/client.py` validates allowed/default model aliases and owns synchronous llama.cpp chat-completion calls, timeouts, optional API-key authentication, finite output limits, generation options, and optional JSON response schemas. It logs request start, completion, timeout, and HTTP-wait duration with model alias, prompt/output sizes, limit, timeout, and image presence, but never prompt or completion content.
- `llm/responses.py` contains compact, cache-friendly prompt construction and fallback behavior for teases, mentions, summons, inactivity nudges, birthday greetings, and varied price-change commentary. Stable rules precede dynamic user, history, and price data. Short teases, summons, inactivity nudges, price messages, and birthday greetings use a 96-token generation budget. Wishlist price increases and decreases receive one generated reaction in a randomly selected funny, mock-corporate, playful, serious, enthusiastic, or melodramatically sad tone; exact prices remain in deterministic notification text.
- `llm/capacity.py` coordinates one active action and two interactive waiters
  across processes using kernel-owned POSIX locks and FIFO tickets. Requests
  automatically acquire admission unless already inside a reserved action.
- `llm/worker.py` owns the persistent bot queue and reservation cleanup for
  mentions and reactions.
- Mention prompts require a grounded new answer. Structured validation rejects
  malformed, empty, or obviously echoed completions and retries once.

## Background tasks

| Task | Default frequency | Purpose |
|---|---:|---|
| Reminder delivery | 10 seconds | Deliver and delete due reminders |
| Daily joke check | 30 seconds | Send one scheduled joke per configured guild/day |
| Birthday check | Daily at 00:01, then 30-minute failure retries | Send due per-user birthday greetings once successfully delivered |
| Inactivity check | 30 minutes | Nudge enabled guild channels quiet for 24 hours |
| Sponsor maintenance | 1 hour | Warn about and clear expired sponsorships |
| Flight tracker | 5 hours | Check at most one tracker per user |
| Wishlist scraper | 12 hours | Refresh price/stock and issue alerts |
| Exchange rates | 24 hours | Refresh EUR-relative conversion rates |

Discord task loops run once shortly after being started. Features contain guards where an immediate post-restart run could waste quota or duplicate work.

Teases, ordinary reactions, inactivity nudges, birthday greetings, and optional
price-change prose are opportunistic under shared
capacity. A skipped birthday does not mark delivery complete; skipped inactivity
does not reset activity. Their existing schedulers retry eligible work. Price notifications continue with deterministic text when
generated prose is unavailable.

## Persistence model

The `db` package owns all SQL, schema setup, migrations, and small mapping helpers.
Queries are grouped by domain while `db/__init__.py` preserves the public
function API. It opens a fresh SQLite connection per operation, enables
foreign-key enforcement, and explicitly bounds lock waits to five seconds.
Initialization enables WAL before beginning transactional schema migrations.
Setup, migration, and commit failures roll back and close the connection;
initialization logs and re-raises failures, stopping bot startup. API lazy
initialization returns HTTP 503 on failure and retries on a later request.

`db.bot_data.add_response` returns a success boolean and invalidates its keyword
cache only after commit. Discord reports failed saves; `/keywords/add` returns
HTTP 500 on failure. Wishlist insertion preserves `None` for a duplicate,
`False` for a database error, and an integer ID for success.

`db/birthdays.py` owns the birthdays table's create/replace, lookup, due-date,
conditional-delivery-marker, and delete helpers. Replacements receive a new
revision while preserving the last successful delivery year; all SQL remains
inside the database layer.

Main tables:

| Table | Scope and purpose |
|---|---|
| `responses` | Guild-specific keyword/response pairs |
| `keyword_usage` | Guild- and user-specific keyword statistics |
| `reminders` | Reminder ownership, lifecycle, timezone, recurrence, retry, and delivery state |
| `reminder_occurrences` | Claimed occurrence identity and Discord delivery reconciliation |
| `birthdays` | One recurring birthday registration per Discord user, including destination and delivery marker |
| `jokes` | Global joke pool |
| `guild_joke_config` | Per-guild schedule and last-send date |
| `guild_joke_sent` | Per-guild no-repeat joke history |
| `settings` | Global key/value configuration |
| `assistant_profiles` | One global preference row per Discord user |
| `action_executions` | Idempotency claims for Discord messages, buttons, modals, and slash interactions |
| `scraped_items` | Per-user wishlist entries, alert preferences/state, and latest check status |
| `price_history` | Wishlist price observations |
| `exchange_rates` | EUR-relative cached rates |
| `flight_api_credentials` | Per-user SerpApi key |
| `flight_trackers` | Per-user fixed-date watches and latest result |
| `flight_price_history` | Historical flight observations |
| `notification_outbox` | Durable wishlist/flight delivery queue, coalescing, retries, and accepted DM parts |
| `guild_activity` | Latest known message time/channel per guild |
| `guild_inactivity_config` | Per-guild switch for LLM inactivity nudges |
| `analytics_daily` | Compact UTC daily activity totals by category and scope |
| `analytics_commands` | Active and historical Discord command catalog |
| `analytics_metadata` | Analytics tracking start metadata |
| `feature_interactions` | Per user/feature/scope/guild counts of wishlist, `/worth-it` and flight slash commands, first/last use, last command and display names |

Foreign-key cascades remove wishlist and flight histories when their owning item/tracker is deleted. Joke sent-history rows are also deleted when a joke is deleted.

Keyword response maps use a 30-second, per-guild, in-process cache. Mutations in the same process invalidate it immediately. Changes made by the Flask process can remain invisible to the bot until the bot cache expires.

SerpApi keys are stored as plaintext in SQLite. Filesystem and backup access therefore need to be restricted.

## Flask API surface

Protected data/configuration routes require a configured bearer token and return HTTP 503 when `API_TOKEN` is absent. Dashboard/static assets and `/health` are public. Mutation JSON must be an object and contain only finite numbers; generic settings cannot bypass sponsor-tier or mention-model validation.

### Keywords

- `POST /keywords/add`
- `DELETE /keywords/delete`
- `GET /keywords/get?guild_id=...`
- `GET /keywords/top?guild_id=...&user_id=...&limit=...` (user/limit optional)

### Reminders

- `POST /reminders/add`
- `DELETE /reminders/delete/<id>`
- `GET /reminders/all`
- `PATCH /reminders/<id>` (message, UTC timestamp or `local_time`, timezone, recurrence)
- `POST /reminders/<id>/retry`

Reminder responses retain the established creation fields and add lifecycle
metadata: creator, timezone, captured language, recurrence, state, attempt
count, next attempt, and failure information. API bearer authentication remains
administrative; Discord interactions derive identity from the interaction and
apply creator/recipient ownership checks.

### Birthdays

- `GET /birthdays`
- `PUT /birthdays/<user_id>` (`channel_id`, optional `guild_id`, `month`, and `day`)
- `DELETE /birthdays/<user_id>`

The dashboard lists all birthday registrations and lets the operator save or
remove the selected user's date and destination. A missing or null `guild_id`
selects a DM destination. API writes preserve the same annual delivery marker
and date validation used by `/set-birthday`.

### Jokes

- `GET /jokes`
- `GET /jokes/<id>`
- `POST /jokes`
- `PUT /jokes/<id>`
- `DELETE /jokes/<id>`
- `POST /jokes/reset`
- `GET /jokes/guilds`
- `GET /jokes/guilds/<guild_id>`
- `PUT /jokes/guilds/<guild_id>`
- `DELETE /jokes/guilds/<guild_id>`

### Wishlist

- `POST /wishlist/add`
- `DELETE /wishlist/remove`
- `GET /wishlist/all`
- `GET /wishlist/preferences?user_id=...&url=...`
- `GET /wishlist/history?user_id=...&url=...` (item metadata and chronological
  saved price observations)
- `PUT /wishlist/preferences` (user_id/url plus target price/currency,
  clear_target, and/or restock_only)
- `POST /wishlist/refresh` (user_id and one url)

Adding an item performs the scrape inline and rejects blocked, unsupported, or
busy results before insertion. Successful insertion returns HTTP 201;
duplicates return 409, database write failures return 500, unsupported pages
return 422, blocked/unreachable targets return 502, and temporary extraction
contention returns 503. Refresh also returns 503 for contention and preserves
the existing snapshot and history on failed checks.

### LLM settings and inactivity

- `GET /llm/mention-model` (current alias and configured allow-list)
- `PUT /llm/mention-model` (validated model alias)
- `GET /inactivity/guilds/<guild_id>`
- `PUT /inactivity/guilds/<guild_id>` (boolean enabled)

### Flights

- `GET /flights/credentials?user_id=...` (status/timestamp, never the key)
- `POST /flights/credentials` (user_id/api_key; validates before storage)
- `DELETE /flights/credentials` (user_id)
- `GET /flights/trackers?user_id=...`
- `POST /flights/trackers` (requires stored credentials; no immediate search)
- `GET /flights/trackers/<tracker_id>?user_id=...`
- `DELETE /flights/trackers/<tracker_id>` (user_id)
- `GET /flights/trackers/<tracker_id>/history?user_id=...`
- `PATCH /flights/trackers/<tracker_id>` (optional budget in the tracker currency)
- `GET /flights/airports?q=...`

In Postman, configure the collection Bearer Token from `API_TOKEN`, use
`http://<HOST>:<PORT>`, and send JSON bodies. GET filters use query parameters.

### Settings

- `GET /settings/<key>`
- `PUT /settings/<key>`

### Profiles and notifications

- `GET /notifications?user_id=...`
- `POST /notifications/<id>/retry`

Profile and notification fields are additive and machine-readable. The
dashboard localizes presentation without translating stored user content or
API field names.

### Analytics

- `GET /analytics/summary?period=7d|30d|90d|all&guild_id=...` (`guild_id` optional)
- `GET /analytics/delivery`
- `GET /interactions?scope=guild|dm` (`scope` optional)

Analytics, like other protected data routes, is disabled with HTTP 503 when
`API_TOKEN` is unset. With a configured token it always requires the matching
Bearer credential, making the report deployer-only. The response includes UTC
date bounds, category and scope totals, active/unused/inactive command rankings,
other feature totals, latest-use timestamps, and a zero-filled daily series.
`/analytics/delivery` reports global reminder and notification counts by
delivery state. `/interactions` lists wishlist, `/worth-it` and flight slash
command usage per user and server or DM, ordered by last use; DM rows return
`guild_id` as null and an invalid scope returns 400. Autocomplete is not
counted and recording failures never block a command.

### Dashboard and host stats

- `GET /health` returns public API/database status, HTTP 200 on success or 503 on database unavailability.
- `GET /` serves the local dashboard; its assets live under `/static/`.
- `GET /system/stats` returns nullable CPU, temperature in Celsius, RAM, disk,
  host uptime, platform, timezone, and collection-time fields using `psutil`.

The dashboard covers keywords, reminders, jokes and schedules, wishlist items
and charts, flight credentials/trackers/charts, airport autocomplete, optional
flight budgets, reminder lifecycle editing, mention-model selection,
inactivity configuration, arbitrary
setting lookup, keyword rankings, and deployer-only
analytics. Overview shows live CPU, temperature, memory, disk, and host uptime;
it polls these host stats every 15 seconds only while Overview is visible.
Analytics offers 7/30/90-day and all-time periods, optional server scope,
command rankings, unused and inactive commands, feature totals, failure counts
by operation, delivery health, and a daily
chart. Users lists wishlist and flight usage per user and server or direct
message, filterable by scope, and opens the selected user's wishlist or flights.
Notifications shows the selected user's pending and failed tracking
notifications. Its English/Romanian selector is stored in browser storage and is
independent of the selected Discord user's profile. It is intended for a LAN deployment with
`HOST=0.0.0.0` and a
configured `API_TOKEN`. The login screen validates the token against
`/system/stats`, retains it only in browser session storage, sends it as a
Bearer token on every API call, and clears it on logout or a 401 response.
The HTML/static assets remain public so the login screen can load; every data
route remains bearer-protected.

Birthday management is user-scoped and includes the date, channel or DM
destination, and last successful delivery year. Keyword responses are displayed
in groups: each configured response has its
own delete action, while one `Delete keyword` action removes every response in
that group. Long responses wrap instead of being truncated. The dashboard uses
browser confirmation for destructive actions and refreshes the affected view
after successful changes.

Dashboard requests send Discord IDs as decimal strings and request string ID
responses with `X-Discord-ID-Format: string`, avoiding JavaScript precision
loss. Existing API clients keep numeric responses when the header is absent.
Database record IDs remain numeric.

## Logging and configuration

### Local LLM deployment

The project previously used Ollama but now talks directly to llama.cpp's
OpenAI-compatible `/v1/chat/completions` API through `llm/client.py`.
`ollama_client.py` and the `*_OLLAMA_*` configuration names were removed.

The reported production deployment as of 2026-11-04 uses a 10 GB service budget
on the Intel N150 and Gemma 4 E2B Q8_0 weights, a 32,768-token context,
three CPU threads, one parallel slot, the model's matching vision projector, and
`--reasoning off`, so the bot sends no reasoning setting. Confirm `/props`
advertises vision after any model change; image recognition still requires a
live visual check.
`--no-mmproj-offload` keeps projector work on the CPU-only host;
`--no-mmproj` must not be present because it disables vision. Prompt caching and
64-token chunk reuse complement the cache-friendly application prompts:

```bash
Environment=HOME=/home/robertivan
Environment=LLAMA_CACHE=/home/robertivan/.cache/llama.cpp

ExecStart=/home/robertivan/.llama-app/llama serve \
  -hf unsloth/gemma-4-E2B-it-GGUF:Q8_0 \
  --no-mmproj-offload \
  --alias gemma4:e2b \
  --host 127.0.0.1 \
  --port 8080 \
  --ctx-size 32768 \
  --parallel 1 \
  --threads 3 \
  --threads-batch 3 \
  --flash-attn auto \
  --cache-prompt \
  --reasoning off \
  --swa-full \
  --cache-reuse 64 \
  --cache-ram 1024
```

Three threads leave one of the N150's four cores available to the OS, Discord
bot, and API. Giving llama.cpp all four cores can make the host unresponsive
during inference even when the Python queue and event loop are healthy.

`-hf` downloads and caches the GGUF in `LLAMA_CACHE`.
For this Gemma repository it also selects the matching multimodal projector
automatically. The alias must match
`LLAMA_CPP_DEFAULT_MODEL`/`LLAMA_CPP_ALLOWED_MODELS`. Binding to `127.0.0.1`
keeps the unauthenticated inference endpoint local to the host.

An earlier observation with Gemma 3 4B Q4_K_M at a 2,048-token
context found that the service used about 1.8 GB peak RAM without swapping. A
representative request
processed roughly 360 prompt tokens at 18.5 tokens/second and generated at
about 7.5 tokens/second. Prompt evaluation was therefore the dominant latency.
`llm/responses.py` now keeps each instruction compact and places reusable rules
before changing history, user, and price fields so llama-server can retain a
larger common prefix between requests. The current 32,768-token context permits
longer combined prompts and output but does not itself make inference faster.

The reported `systemd` service runs as `robertivan`, with
`Restart=on-failure`, `RestartSec=5`, `MemoryMax=10G`, and the command above in
`ExecStart`. After changing the unit, run `systemctl daemon-reload` followed
by `systemctl restart llama-server`; inspect it with
`systemctl status llama-server` and `journalctl -u llama-server`.
After restarting llama-server, inspect `http://127.0.0.1:8080/props` and confirm
that `modalities.vision` is `true` before restarting the Discord bot. During the
first PNG description and screenshot-question smoke tests, record cold-start
and image-processing latency and confirm the service remains below its 10 GB
limit.

By default, llama-server keeps the model loaded indefinitely
(`--sleep-idle-seconds -1`). Setting `--sleep-idle-seconds 600` unloads the
model weights and KV cache from RAM after ten minutes without an inference
request; use `300` for five minutes. A new request wakes the server and reloads
the model automatically, so its first response has cold-start latency.
`MemoryMax=8G` is only a hard systemd memory limit and does not provide idle
unloading. Confirm that the installed llama.cpp build supports the option with
`llama-server --help | grep sleep-idle`.

llama.cpp has no direct Ollama `Modelfile` equivalent. The model path/Hub
reference, alias, context window, and generation options are configured via
server flags and request JSON. Client requests are user-only unless a feature
opts into a system instruction; mention answers and summons do so to request
direct final output without thinking blocks. Persistent behavior can be
supplied by GGUF chat-template metadata or `llama-server --chat-template-file`.
Because this server is also used for structured scraper extraction, a global
personality template affects both conversational and JSON-extraction requests;
use a separate server/model or a feature-specific prompt if those requirements
must differ.

`logger.py` creates console and rotating-file handlers. The bot and API use
separate logger/file names. `LOG_DIR` controls the file location and
`LOG_LEVEL` controls console/file verbosity, defaulting to `INFO`.
LLM diagnostics record queue admission or rejection, scheduler skip reasons,
HTTP lifecycle events, token/cache usage, and
llama-server prompt and generation timings. Each request receives a short
correlation ID, while every line includes the logger name, process ID, and
thread name. Logs include sizes and Discord/database IDs where useful but
exclude message, prompt, completion, and image contents.

Important environment values include:

- `DISCORD_TOKEN`
- `BOT_ID`
- `HOST`
- `PORT`
- `API_TOKEN`
- `DB_FILE`
- `LOG_DIR`
- `LOG_LEVEL`
- `LLAMA_CPP_BASE_URL`
- `LLAMA_CPP_DEFAULT_MODEL`
- `LLAMA_CPP_ALLOWED_MODELS`
- `MENTION_LLAMA_CPP_MODEL`
- `LLAMA_CPP_TIMEOUT`
- `LLAMA_CPP_VISION_TIMEOUT`
- `LLM_IMAGE_MAX_EDGE`
- `LLM_CAPACITY_DIR`
- `LLAMA_CPP_API_KEY`
- `ASK_COOLDOWN_SECONDS`
- `LLM_CONTEXT_MESSAGES`
- `LLM_MENTION_MAX_TOKENS`
- `LLM_REACTION_CHANCE`
- `LLM_REACTION_COOLDOWN_SECONDS`
- `TEASE_LLM_ENHANCE`
- `TEASE_LLAMA_CPP_MODEL`
- `TEASE_LLAMA_CPP_TIMEOUT`
- `SERPAPI_BASE_URL`
- `SERPAPI_ACCOUNT_URL`
- `SERPAPI_TIMEOUT`
- `SERPAPI_GL`
- `SERPAPI_HL`
- `FLIGHT_CHECK_INTERVAL_HOURS`
- `FLIGHT_CHECK_GAP_SECONDS`

The bot refuses to start without its Discord and required llama.cpp model configuration. The API refuses to start without `HOST` and `PORT`, but does not require `API_TOKEN`.

`LLM_CAPACITY_DIR` defaults to `.llm-capacity` beside the configured `DB_FILE`;
Compose sets `/data/.llm-capacity` for both services. It must point to the same
writable local directory in every bot/API process. Coordination supports one
macOS/Linux host and a shared local volume; multi-host and network-filesystem
coordination are outside scope. The queue deadline defaults to
`LLAMA_CPP_TIMEOUT` (180 seconds), and the HTTP timeout starts after activation.
Kernel locks recover on process exit. A killed or timed-out HTTP client cannot
prove that llama-server has stopped its remote computation.

## Tests and current verification status

The test suite mirrors the primary service boundaries and contains coverage for:

- Database CRUD, migrations, cache invalidation, and cascading deletes
- Keyword response selection
- Mention parsing, single-slot admission, message splitting, image selection, and
  deferred attachment downloads
- Assistant-profile defaults, validation, partial updates, and LLM preference
  precedence
- llama.cpp client configuration, request options, vision capability checks,
  and base64 multimodal payloads
- Tease/mention prompt behavior, including image-only descriptions and
  caption-grounded vision requests
- Birthday parsing, command registration, private save/delete behavior,
  destination replacement, due-date delivery, retries, race guards, mentions,
  analytics, and state-write failures
- Contextual reaction selection, cooldowns, worker-idle behavior, and permission failures
- Scraper URL validation and extraction methods
- Currency conversion
- Price alert state transitions and currency-aware target checks
- LLM price-change direction, tone selection, and factual-DM fallback
- Flight provider parsing and tracker behavior
- Inactivity helper logic
- Sponsor plan rendering from canonical tier values
- Statistics formatting
- Romanian calendar parsing, Python 3.9 command registration, and user-facing behavior

The local `.venv` used for verification is Python 3.9.6 and is gitignored.
Docker and test CI use Python 3.13. Discord slash-command callbacks use
`typing.Optional` rather than PEP 604 unions where required, so they register
correctly under Python 3.9.

Reliability checks use the repository `.venv` (Python 3.9.6) and cover shared
capacity across processes, cancellation, contention skips, scraper evidence,
SQLite initialization retry/rollback, and truthful write failures. The existing
urllib3/LibreSSL compatibility warning remains. Tests use isolated databases; no live Discord messages
or production services were used.

Verification on 2026-10-09: the integrated targeted run passed 601 tests; the full suite ran once after implementation and passed all 1,323 tests in 24.80 seconds. `git diff --check` and syntax parsing of all 36 changed Python files passed. The existing urllib3/LibreSSL warning is unchanged. Docker build/Compose validation was unavailable because the local environment has no Docker CLI. No dependencies were changed, and no live Discord or provider requests were made. New tests cover SSRF/DNS/redirect/download limits, transactional wishlist writes, credential redaction, fail-closed auth, public health, recurring recovery/cleanup, flight enqueue rollback, tracker quota selection, notification loop recovery, and isolated startup.

`tests/test_api.py` covers API configuration and summary routes.
`tests/test_dashboard.py` covers dashboard asset delivery, bearer protection on
new data routes, exact Discord ID serialization, wishlist history scoping and
ordering, and nullable/available host metrics including temperature.
`tests/test_wishlist_graphs.py` covers custom periods, requester scoping,
button/modal updates, missing data, invalid baselines, error recovery, and
slash-option registration. `tests/test_chart_renderer.py` covers real PNG
rendering, duplicate titles, median comparison, product caps, and time-axis formatting.
Mention tests cover explicit requester tags and one-ping multipart replies;
command-naming tests cover the hyphen convention.

CI has both a Python 3.13 pytest workflow and a Docker build/Compose-validation
workflow. Neither workflow starts a live Discord bot.
## Notable implementation risks and inconsistencies

### Python version compatibility

Most postponed PEP 604 annotations import under Python 3.9, but discord.py evaluates slash-command callback annotations at registration time. Such callbacks use Python-3.9-compatible typing constructs such as `Optional[...]`.

### Response gate mismatch

Comments say the keyword and tease systems share a cooldown, but only keyword replies check and update `ResponseGate`. A random tease can therefore occur without observing the advertised shared cooldown.

### SQLite concurrency

WAL and explicit five-second lock waits reduce contention between the Discord
and Flask processes. SQLite still serializes writers; writes can fail when
contention outlasts the bounded wait.

### Database initialization errors

Initialization failures now propagate to bot startup and produce retryable API
503 responses. Keyword and wishlist adds distinguish successful writes from
failures. Other DB mutation helpers retain their existing return conventions;
their callers still need individual review when changing persistence behavior.

### API authentication default

Missing or blank `API_TOKEN` makes protected routes unavailable with HTTP 503. Configure a token before using the dashboard data/API after deployment; public HTML/static assets and the database health probe still load.

### Command authorization

Most mutating slash commands do not enforce Discord administrator or role permissions. Any guild member able to use the commands may add keyword responses, change global LLM settings, add jokes, or change that guild's joke schedule. Sponsor changes use their own password modal.

### Secret storage

Per-user SerpApi keys are stored unencrypted in SQLite. Logs avoid printing the keys, but database files and backups remain sensitive.

### Synchronous database calls in async paths

Inactivity activity writes, keyword lookups/usage writes, action admission, and frequent tracking/delivery operations now use worker threads. Some less frequent handlers and helper reads remain synchronous; SQLite still serializes writes and a thread can wait up to five seconds under contention.

### Reminder delivery semantics

Due reminders are claimed transactionally and remain persisted after delivery or
failure. Transient errors retry after one, five, and fifteen minutes; permission
errors become visible failed records. Uncertain sends are reconciled from the
occurrence marker before an explicit retry. Delivery is not promised exactly
once across a network failure window.

## Practical change guide

When extending the project:

- Add Discord commands and event behavior inside a focused `features/` class.
- Register the feature in `bot.py`.
- Add it to `MESSAGE_HANDLERS` only if it observes ordinary messages.
- Add it to `BACKGROUND_FEATURES` only if it implements an idempotent `start_tasks()` method.
- Keep provider/parsing logic outside feature modules when the API or tests should reuse it without Discord imports.
- Put all persistence operations and migrations in the `db` package.
- Preserve guild/user scoping explicitly; the `settings` table is global.
- Use `asyncio.to_thread` for synchronous HTTP, LLM, heavy graph, or other blocking work invoked from Discord handlers.
- Make scheduled loops resilient per item so one bad record does not stop a whole pass.
- Add tests next to the corresponding existing test module and run them with the
  repository `.venv` when available; slash callbacks must still register under
  its Python 3.9 runtime.
- Restart both the bot and API after shared database/schema changes.
