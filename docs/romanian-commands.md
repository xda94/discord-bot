# Bilingual commands and delivery

The deterministic parser runs before the model worker. Every text request and
clarification needs an actual bot mention, including in DMs. Successful natural
actions receive only ✅. Lists, product selections, and graphs are private.
Buttons and modals do not require a mention and check the requesting user's
ownership. `/start` provides private language/timezone setup and feature help;
onboarding is never sent automatically.

## Examples

Replace `@bot` with a real mention and IDs with IDs from your private lists.

| Romanian | English |
| --- | --- |
| `@bot adu-mi aminte peste o oră să sun acasă` | `@bot remind me in one hour to call home` |
| `@bot pune-mi un reminder peste 1 oră și 30 de minute să verific cuptorul` | `@bot remind me in 1 hour and 30 minutes to check the oven` |
| `@bot amintește-mi mâine la 09:00 să sun acasă` | `@bot remind me tomorrow at 09:00 to call home` |
| `@bot amintește-mi zilnic la 09:00 să beau apă` | `@bot remind me daily at 09:00 to drink water` |
| `@bot arată-mi lista de dorințe în lei` | `@bot show my wishlist in RON` |
| `@bot anunță-mă când căștile scad sub 500 lei` | `@bot notify me when headphones drop below 500 RON` |
| `@bot actualizează #12` | `@bot refresh #12` |
| `@bot nu mai urmări #12` | `@bot stop tracking #12` |
| `@bot arată-mi reminderele` | `@bot show my reminders` |
| `@bot modifică reminderul #4 la mâine la 10:00` | `@bot edit reminder #4 to tomorrow at 10:00` |
| `@bot anulează reminderul #4` | `@bot cancel reminder #4` |
| `@bot amână reminderul #4` | `@bot snooze reminder #4` |

Politeness, diacritics, legacy ş/ţ, and sentence punctuation are accepted.
Matching normalizes text; reminder text and product URLs retain their original
content. Supported durations include minutes, hours, days, weeks, fractions,
and compounds joined by `și` or `and`. Calendar inputs accept today/tomorrow,
weekdays, `YYYY-MM-DD HH:MM`, and `DD.MM.YYYY HH:MM`. Vague times, past times,
and ambiguous/nonexistent local times request an exact replacement.

Amounts follow the request's language: Romanian `1500,50` / `1.500,50` and
English `1500.50` / `1,500.50`. A lone grouping separator with three trailing
digits is ambiguous: write an ungrouped amount instead. RON/lei/leu and EUR/euro
are aliases. Product references resolve only within the requester's wishlist.
Ambiguous names produce private choices; “ăsta” requires an explicit bot reply
or a single recent owned result.

One clarification is held in memory per user/channel for ten minutes. Restart
forgets it. Send a mentioned `cancel` / `anulează` to abandon it. For reminder
text, reply directly to the clarification with a mention, or use a mentioned
`text: ...` / `mesaj: ...` response. Unrelated conversation does not fill it.

## Preferences and tracking

Language precedence is explicit request override, saved preference, request
language, English. Scheduled reminders use the saved preference or their
captured creation language. Set an explicit IANA timezone with `/start` or
`/assistant-profile-set`; legacy UTC defaults need one confirmation. Saved
non-UTC profiles count as configured.

`/remind` preserves its `when`, `who`, and `what` arguments and adds optional
`recurrence` (`daily`, `weekdays`, `weekly`). `/reminder-list`, `/reminder-edit`,
`/reminder-cancel`, and `/reminder-snooze` expose ownership-checked controls.
Calendar edit forms also require an explicit profile timezone. Creator and
recipient can manage a reminder. Snooze creates an independent
one-off and does not shift a recurring schedule. Monthly rules are deferred.

`/flight-tracker-add` accepts an optional budget in the existing tracker
currency. `/flight-tracker-budget` sets or clears it. Budget alerts re-arm after
the price rises above the threshold; trackers without a budget keep their
previous first-price/drop behavior. Airport autocomplete uses the attributed
checked-in catalog in `data/AIRPORTS.md`; ambiguous cities require selection.
Fixed dates, cadence, and provider quota limits are unchanged.

Profiles add `quiet_start`, `quiet_end`, `delivery_mode` (`immediate` or `daily`),
and `digest_time`. Both scheduling options require an explicit timezone.
Quiet hours need both HH:MM bounds and can span midnight. Defaults remain
immediate with no quiet hours. These controls affect tracking alerts; reminders
bypass them. Durable tracking updates coalesce while waiting, with separate
preserved target/restock events. Daily output labels observation times rather
than claiming a new price check. A large daily digest can span multiple DMs.

## Delivery and migrations

Startup applies idempotent additive SQLite migrations. Back up the database
before deployment. Old reminders stay one-off, existing flight trackers have no
budget, and existing profiles keep immediate alerts. Existing API routes and
response fields remain; new optional lifecycle/profile/budget fields are added.
Bearer-token administration retains its existing semantics.

Reminders retain pending, delivered, failed, and cancelled records. Due
occurrences are claimed transactionally. Transient errors retry after 1, 5,
and 15 minutes; permission failures stop and remain visible. Stale or uncertain
sends become failed records. Reminder reconciliation checks recent channel
history for a persisted occurrence marker. Explicit retry is available in lists
and the dashboard. Terminal records and delivered occurrence history are kept
for 30 days. On restart, a missed one-off delivers once; recurring misses
coalesce into one overdue delivery before advancing to the next future local
occurrence. A nonexistent future recurring clock time is skipped at delivery.

The notification outbox persists failed DMs and accepted message-part progress.
Uncertain sends are visible failures rather than blind retries. Discord/network
failure windows mean delivery is not guaranteed exactly once: if the server
accepted a send before its receipt was persisted, explicit retry can duplicate
it. Review uncertain failures before retrying.

Additional authenticated routes:

- `PATCH /reminders/<id>`: `remind_at`, `message`, `timezone`, `recurrence`; optional `local_time` resolves in the reminder timezone.
- `POST /reminders/<id>/retry`.
- `GET/PATCH /assistant-profiles/<user_id>`.
- `PATCH /flights/trackers/<id>`: optional `budget`.
- `GET /flights/airports?q=...`.
- `GET /notifications?user_id=...`, `POST /notifications/<id>/retry`.
- `GET /analytics/natural?period=30d&guild_id=...`.

The dashboard language selector persists in browser storage and is independent
of Discord profile language. API identifiers and stored user content remain
unchanged. Analytics store daily aggregate counters only, split by intent,
language, parser/model route, and outcome. No request text, URLs, reminder
content, or model prompts are collected for these counters.

For future recurring occurrences, a nonexistent local clock time is skipped;
an overlapping clock time uses its first occurrence. New calendar requests
for either case require clarification. Timeout metrics and explicitly abandoned
clarifications are counted separately.

## Rollout and evaluation

`NATURAL_LLM_ENABLED` defaults to disabled. Set it to `1` only after the deployed
model passes the reviewed evaluation. The worker retains its existing one-slot
admission and cooldowns; deterministic commands and clarification completion do
not depend on model availability. Model proposals are allowlisted, validated,
and checked against the current request before execution. Quoted/replied
content cannot authorize an action or invent an operand.

Run:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python scripts/evaluate_natural_commands.py
# Requires the configured llama.cpp server and allowed model aliases:
.venv/bin/python scripts/evaluate_natural_commands.py --live --model discord-bot
```

The evaluation tool is read-only: it never runs actions or sends Discord
messages. It reports aggregate accuracy and unauthorized action counts, with a
95% threshold across at least 100 cases and zero unauthorized outcomes. Passing
the parser corpus does not certify the model. Keep model actions disabled when
live evaluation is unavailable or fails.

Deploy deterministic/localization changes first, then reminders, tracking and
notification controls, dashboard, and analytics. Quiet hours/digests are opt-in.
Before releasing to users, exercise mentions, private selections, recurring
reminders, failed deliveries/retry, and language switching in a test Discord
server. The local automated suite does not replace that live acceptance check.
