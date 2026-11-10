# Improvement plan

Written 2026-11-10 from a review of `main` at `5b08dc4`. Baseline: `.venv/bin/python -m pytest -q` → 975 passed in about 20 s.

This file is the implementation contract for delegated agents. Each work item is self-contained: give an agent the "Rules for every agent" section plus its own item.

## Rules for every agent

- Do only the assigned work item. Touch only the files listed under "Owns". If another file must change, stop and report it.
- Keep behaviour identical unless the item says otherwise. No refactors, renames, reformatting, new dependencies or comment/docstring additions beyond what the item names.
- The local venv is Python 3.9.6; Docker and CI use 3.13. Code must run on both (keep `from __future__ import annotations`; no `match`, no `X | Y` at runtime).
- Run the targeted tests named in the item, then `.venv/bin/python -m pytest -q` once at the end. All 975 existing tests must pass, plus any the item adds.
- Do not commit. Report files changed, tests run with results, and any deviation from the item.

## Findings in brief

| # | Area | Finding | Impact |
|---|------|---------|--------|
| 1 | Reminders loop | Every 10 s the loop loads every reminder row (`store.list_for()`) to find a rare failed state, then runs four `DELETE`s (`store.cleanup`) | Constant write-lock churn and a full scan 8,640 times a day |
| 2 | Message hot path | Every human message does a thread hop, a new SQLite connection and a commit (`set_guild_activity`), then another thread hop for keywords even on a cache hit, then compiles one regex per keyword | Cost scales with message volume × keyword count |
| 3 | Event loop | About 60 SQLite calls run directly on the event loop in `jokes`, `birthdays`, `flights`, `reminders`, `onboarding`, `help_feature`, `llm_mention`, `inactivity` while the rest of the code uses `asyncio.to_thread` | A locked database (5 s timeout) freezes the whole bot, including heartbeats |
| 4 | LLM capacity | A queued job polls every 10 ms, each poll being a thread dispatch plus about five file opens and locks, for up to `LLAMA_CPP_TIMEOUT` (180 s) | Roughly 100 thread hops per second per waiting job |
| 5 | Dead code | `web/routes/administration.py` holds stale copies of `_serialize_scraped_item` and `_validate_flight_tracker_payload`; the latter and line 55 reference names that are never imported (`os`, `normalize_iata`, `parse_iso_date`, `_is_int`, `SUPPORTED_CURRENCIES`) | Latent `NameError`; nothing catches it because there is no linter |
| 6 | Tooling | No linter, no `.env.example` (33 env vars), unpinned dependency ranges with no lock file, CI runs twice on PR branches, root `package.json` is an empty `{}` | Unreproducible builds, undetected undefined names |
| 7 | API serving | `api.py` serves with Flask's development server (`app.run(threaded=True)`); no container healthcheck although `/health` exists | Not meant for production use |
| 8 | Database | No index on `keyword_usage` (queried by guild, and by guild + user); `price_history` is read by item ordered by timestamp but has two single-column indexes; one scrape pass commits 3–5 separate transactions per item | Slow stats queries as data grows; partial state if the process dies mid-item |
| 9 | Multi-guild state | `ResponseGate` cooldown, tease mood and the daily tease counter are process-wide, so one busy server throttles keyword replies in every other server; teases also fire in DMs | Behaviour question, see "Decisions" |
| 10 | Docs and style | README test section is stale (says 1,323 tests); `db/reminders.py`, `db/notifications.py`, `notifications.py` and `features/reminders.py` are written without spaces with lines over 300 characters; 95 f-string log calls | Maintainability only |

Things that were checked and are in good shape: SSRF protection and download limits in `wishlist/scraper.py`, constant-time bearer-token comparison, log redaction, test isolation from the real database, WAL mode, durable notification outbox.

## Decisions for the owner

These change behaviour or add dependencies, so no agent should pick them. Recommended option first.

1. **Per-guild cooldown and tease state (WI-9).** Make `ResponseGate`, tease mood and tease counter per guild (recommended), or keep them global.
2. **Production server for the API (WI-7).** Add `waitress` (pure Python, recommended), or keep the Flask development server.
3. **Python version.** Recreate the local venv on 3.13 to match Docker and CI (recommended), or keep supporting 3.9.
4. **Dependency pinning (WI-6).** Add a `pip-compile` generated `requirements.lock` used by Docker and CI (recommended), or keep ranges.
5. **SerpApi keys.** They are stored in plain text in `flight_api_credentials.api_key`. Encrypting them needs a key in `.env`; not planned unless requested.

## Decisions made (2026-11-10)

1. Per-guild cooldown and tease state: yes (WI-9, folded into WI-2's agent because both own `features/keywords.py`).
2. Keep the Flask development server: WI-7 is dropped.
3. Keep Python 3.9 support.
4. Add a hashed `requirements.lock` (WI-6 step 6).
5. Encrypt SerpApi keys: new WI-13 below.
6. WI-12 (joke picker) is folded into WI-2's agent (same file, `db/bot_data.py`).
7. WI-10 (formatting) and WI-11 (lazy logging) rewrite lines that every other item edits, so they run after the others are merged.

#### WI-13 Encrypt stored SerpApi keys

Owns: `db/flights.py`, new `flight_credentials.py`, new `tests/test_flight_credentials.py`. Does not edit `requirements*.txt` (WI-6 adds `cryptography>=42,<46` there), `db/schema.py` or `features/flights.py`.

1. New module `flight_credentials.py` with `encrypt(api_key) -> str` and `decrypt(value) -> str` using `cryptography.fernet.Fernet`, keyed by env var `FLIGHT_CREDENTIALS_KEY` (a Fernet key). Stored values carry the prefix `enc:v1:`; values without it are legacy plaintext.
2. `db/flights.py`: `set_flight_api_credentials` stores the encrypted form; `get_flight_api_credentials` returns the plaintext key, decrypting when prefixed, and when it reads a legacy plaintext row and the key is configured it re-saves it encrypted. The API of both functions (arguments, return shape) does not change.
3. If `FLIGHT_CREDENTIALS_KEY` is unset: log one warning per process and keep the legacy plaintext behaviour, so existing deployments do not break. If it is set but invalid, or a stored `enc:` value cannot be decrypted, raise a clear error and never log the key or the value.
4. Tests: round trip; legacy plaintext is migrated on first read; unset key falls back; wrong key fails; raw column never contains the plaintext after a save with a key.

## Work items

Batch A items have no dependencies on each other and own disjoint files, so they can run in parallel. Batch B needs the decisions above. Batch C is optional.

### Batch A

#### WI-1 Reminder loop: targeted query and hourly cleanup

Owns: `db/reminders.py`, `features/reminders.py`, `tests/test_runtime_reliability.py` (or a new `tests/test_reminders_loop.py`).

1. In `db/reminders.py` add `list_uncertain()` returning the same row dicts as `list_for()` but with `WHERE state='failed' AND last_error='uncertain-delivery' ORDER BY remind_at,id`.
2. In `RemindersFeature._check` (`features/reminders.py:162`) replace `store.list_for` with `store.list_uncertain` and drop the now redundant `if row['state']=='failed' and ...` test.
3. Run `store.cleanup` at most once per hour: keep a `time.monotonic()` deadline on the instance, initialised in `__init__` so the first pass still cleans up.
4. Initialise `self._reconciled = {}` in `__init__` instead of the `getattr` fallback, and drop entries whose deadline has passed at the start of each pass.
5. Wrap the direct `db.get_assistant_profile(...)` and `store.*` calls in this file's command and button handlers (lines 35, 64, 99, 123, 143, 146, 155, 160, 161) in `await asyncio.to_thread(...)`.

Acceptance: a pass with no uncertain reminders issues no `SELECT` over all reminders and no `DELETE`; delivery, retry, snooze and reconciliation behave as before. Add a test that `list_uncertain` returns only matching rows and one that two consecutive `_check` passes call `cleanup` once.

Must not change: SQL in `claim_due`, `complete`, `fail`; the embed footer marker format.

#### WI-2 Message hot path: debounce activity writes, cache keyword matcher

Owns: `features/inactivity.py`, `features/keywords.py`, `db/bot_data.py` (cache section only, lines 14–98), `tests/test_inactivity.py`, `tests/test_keywords.py`.

1. `InactivityFeature.handle_message`: always update `self._state` in memory, but call `db.set_guild_activity` only when the channel changed or at least 60 s passed since the last persisted write for that guild (track the persisted timestamp in the state dict). The 24 h threshold makes 60 s of restart drift irrelevant.
2. `InactivityFeature` `/llm-inactivity` handler (line 88): wrap `db.set_guild_inactivity_enabled` in `asyncio.to_thread`.
3. `db/bot_data.py`: add `get_cached_responses(guild_id)` that returns the cached dict when the TTL is still valid and `None` otherwise, without touching SQLite. Export it from `db/__init__.py` (that one line is the only permitted edit there).
4. `KeywordsFeature.handle_message`: try `db.get_cached_responses` first and fall back to `asyncio.to_thread(db.get_all_responses, guild_id)` only on a miss.
5. Replace the per-keyword `re.search` with one compiled alternation per guild, cached on the feature keyed by `id()` of the responses dict (the dict object is replaced on every cache refresh, so identity is a valid cache key). Build it as `(?<!\w)(?:k1|k2|...)(?!\w)` with keywords sorted longest first and `re.escape`d. The reply must still be chosen for the first keyword in dict order that matches, as today; if that cannot be guaranteed with a single pattern, use the combined pattern only as a fast "no match" pre-check and keep the existing loop for the match case.

Acceptance: a message in a guild with a warm cache and no keyword match performs zero thread dispatches in `KeywordsFeature`; ten messages within 60 s in one channel produce one `set_guild_activity` call; existing keyword tests pass unchanged.

Must not change: `_pick_response`, sponsor suffix logic, analytics calls, cache TTL.

#### WI-3 Move blocking SQLite calls off the event loop

Owns: `features/jokes.py`, `features/birthdays.py`, `features/flights.py`, `features/onboarding.py`, `features/help_feature.py`, `features/llm_mention.py`, `features/wishlist_graphs.py`, `notifications.py`.

For every direct `db.*` call that executes inside an `async def` (including Discord UI callbacks), change `db.f(args)` to `await asyncio.to_thread(db.f, args)`. Known sites:

- `features/jokes.py`: 58–101 (legacy migration helper), 116, 143, 166, 194, 219, 248–251, 280–281
- `features/birthdays.py`: 96, 135, 166, 264, 287, 313
- `features/flights.py`: 211, 307, 326, 350, 378
- `features/onboarding.py`: 243, 263, 337
- `features/help_feature.py`: 181
- `features/llm_mention.py`: 719, 739, 830, and both `get_selected_model()` call sites (it reads and may write `settings`)
- `features/wishlist_graphs.py`: 228

First check each site's caller: helpers that are already invoked through `asyncio.to_thread` (for example `flights._provider_for_user`, `notifications.prepare_tracking`, `wishlist_graphs` lines 48–63 if their caller is threaded) must stay synchronous. Synchronous helper functions keep their signatures; only the awaiting call site changes. Where a sync helper is called from async code and itself hits the database (`flights.py:47`, `128–142`), wrap the helper call, not its internals.

Acceptance: `grep -nE "\bdb\.[a-z_]+\(" features/*.py notifications.py | grep -v to_thread` lists only lines inside synchronous functions that run in a worker thread, plus `InactivityFeature.__init__`. No behaviour change; existing tests pass (tests that stub `db` functions with plain callables keep working under `to_thread`).

Must not change: `features/reminders.py` and `features/inactivity.py` (owned by WI-1 and WI-2), command names, reply texts.

#### WI-4 LLM capacity: back off while waiting

Owns: `llm/capacity.py`, `tests/test_llm_capacity.py`.

1. `async_action_context`: replace the fixed `asyncio.sleep(0.01)` with a delay that starts at 0.02 s and doubles to a cap of 0.25 s, reset per call.
2. `Reservation.activate` and `_admission`: same back-off for `time.sleep`, capped at 0.1 s.

Acceptance: ordering by ticket, timeout errors and release semantics are unchanged; existing capacity and worker tests pass without raising their own timeouts. Add one test asserting that a waiter blocked for about 1 s calls `try_activate` fewer than 20 times.

Must not change: lock file layout, `reserve`, `busy`, the public names.

#### WI-5 Remove dead code and latent NameErrors

Owns: `web/routes/administration.py`, `package.json`, `package-lock.json`.

1. Delete `_serialize_scraped_item` (line 101) and `_validate_flight_tracker_payload` (line 123) from `web/routes/administration.py`; the live versions are in `web/routes/wishlist.py` and `web/routes/flights.py`. Confirm with `grep` that nothing imports them from `administration`.
2. Add `import os` (used at line 55).
3. Remove imports that become unused after step 1 (`date` if no other use).
4. Delete the root `package.json` (`{}`) and `package-lock.json` after confirming nothing references them (`grep -rn "package.json\|npm" --include="*.yml" --include="Dockerfile" --include="*.md" .`). If the README mentions npm or PM2 installation through them, leave both files and report.

Acceptance: `python -m pyflakes web/routes/administration.py` (install pyflakes into the venv for the check only; do not add it to requirements) reports no undefined names. `tests/test_api*.py` and `tests/test_dashboard.py` pass.

### Batch B (after decisions)

#### WI-6 Tooling and reproducibility

Depends on: WI-5 (so the first lint run is clean). Owns: `requirements-dev.txt`, new `ruff.toml`, new `.env.example`, `.github/workflows/test.yml`, `.github/workflows/docker.yml`, `README.md` (the "Tests" and "Configuration" sections only).

1. Add `ruff` to `requirements-dev.txt`. `ruff.toml`: `select = ["F", "E9"]` only (undefined names, unused imports, syntax errors); no formatter, no style rules. Fix what it reports with the smallest edit; report anything that is not a trivial unused import instead of fixing it.
2. Add a `ruff check .` step to `test.yml` before pytest.
3. In both workflows change `push: branches: ["**"]` to `push: branches: [main]` so PR branches run once.
4. Create `.env.example` listing every variable read through `os.getenv` (33 names; get the list with `grep -rhoE 'getenv\("[A-Z_]+"' --include="*.py" . | sort -u`, excluding `.venv`), grouped as required and optional, with defaults taken from the code and no real values. Make sure `.gitignore` does not exclude it.
5. Replace the dated test narrative in README "Tests" (the paragraphs mentioning 601 and 1,323 tests) with the current commands, including `ruff check .`.
6. Only if decision 4 is "lock file": add `requirements.lock` generated with `pip-compile --generate-hashes` under Python 3.13 and use it in `Dockerfile` and `test.yml`.

Acceptance: `ruff check .` exits 0; CI YAML is valid; pytest passes.

#### WI-7 Serve the API with a production server

Depends on: decision 2. Owns: `api.py`, `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `tests/test_api_initialization.py`.

1. Add `waitress>=3.0,<4.0`. In `api.py` under `__main__`, replace `app.run(...)` with `waitress.serve(app, host=HOST, port=int(PORT), threads=8)`. Keep the module-level `app` object and all startup validation.
2. Add a compose healthcheck for the `api` service that requests `http://127.0.0.1:${PORT:-9999}/health` with Python's `urllib` (the slim image has no curl).

Acceptance: `python api.py` starts and `/health` returns 200; tests importing `api` still pass.

#### WI-8 Database indexes and atomic scrape persistence

Owns: `db/schema.py`, `db/wishlist.py`, `features/wishlist.py` (`_process_scrape_item` only), `tests/test_db.py`, `tests/test_alerts.py`.

1. In `init_db` add `CREATE INDEX IF NOT EXISTS idx_keyword_usage_guild_keyword ON keyword_usage(guild_id, keyword)` and `idx_keyword_usage_guild_user ON keyword_usage(guild_id, user_id)`.
2. Add `idx_price_history_item_ts ON price_history(item_id, timestamp)` and `DROP INDEX IF EXISTS idx_price_history_item_id` (the composite covers it).
3. Add `persist_scrape_pass(item_id, *, price, in_stock, title, currency, target_alerted=..., alert_kind=..., alert_price=...)` in `db/wishlist.py` that performs, in one `BEGIN IMMEDIATE` transaction, what `add_price_history`, `update_scraped_item_target_state`, `update_scraped_item_status` and `update_item_alert_state` do today, using `...` as "leave unchanged". Use it at the end of `_process_scrape_item` (`features/wishlist.py`, the four calls between lines 769 and 869). Keep the ordering guarantee that state is written only after the notification is queued, and keep the early `return` on notification failure.

Acceptance: existing alert tests pass with the same final row state; a new test shows that a failure inside `persist_scrape_pass` leaves the item row and `price_history` untouched. The old single-purpose functions stay, since the API and tests use them.

#### WI-9 Per-guild cooldown and tease state

Depends on: decision 1 and WI-2 (shares `features/keywords.py`). Owns: `features/response_gate.py`, `features/teases.py`, `features/keywords.py` (gate calls only), `tests/test_keywords.py`, `tests/test_tease_llm.py`.

1. `ResponseGate.can_respond(key)` and `mark_responded(key)` keep one timestamp per key (guild id); `KeywordsFeature` passes `guild_id`.
2. `TeasesFeature`: keep `current_mood`, `teases_today` and the reset date per guild in a dict; `/mood` changes the calling guild only; `handle_message` returns early for messages without a guild.

Acceptance: a keyword reply in guild A does not block one in guild B within 10 s; `/mood` in A leaves B's mood unchanged; no tease LLM call for DMs.

### Batch C (optional, low priority)

- **WI-10 Formatting of the four dense modules.** Run `ruff format` on `db/reminders.py`, `db/notifications.py`, `notifications.py`, `features/reminders.py` only, in a commit that contains nothing else. Must run after WI-1 and WI-3 so it does not conflict.
- **WI-11 Lazy logging.** Convert the 95 `logger.x(f"...")` calls to `%s` arguments. Mechanical; one module per agent.
- **WI-12 Joke picker.** `get_unsent_joke_for_guild` loads every unsent joke to pick one; use `ORDER BY RANDOM() LIMIT 1`. Only matters with a large joke pool.

## Suggested order

1. Batch A: WI-1 to WI-5 in parallel (disjoint files).
2. WI-6, so every later change is linted in CI.
3. WI-7, WI-8, WI-9 in parallel once the decisions are made (WI-9 after WI-2 is merged).
4. Batch C if wanted, WI-10 last.

## Not recommended now

- A migration framework or `PRAGMA user_version`: `init_db` is idempotent and runs once per process.
- A connection pool: one short-lived SQLite connection per call is fine at this scale once the hot-path writes are debounced (WI-2).
- Replacing `requests` with an async HTTP client: every blocking HTTP call already runs in a worker thread.
- Making the `db` layer raise instead of returning `[]`/`None`/`False` on errors: a real improvement in principle, but it changes the contract of about 60 functions and their callers.
