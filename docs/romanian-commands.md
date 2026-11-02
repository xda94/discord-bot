# Bilingual commands and delivery

Buttons and modals check the requesting user's ownership. `/start` provides
private language/timezone setup and feature help; onboarding is never sent
automatically.

## Preferences and tracking

Language precedence is explicit request override, saved preference, request
language, English. Scheduled reminders use the saved preference or their
captured creation language. Set an explicit IANA timezone with `/start`;
legacy UTC defaults need one confirmation. Saved non-UTC profiles count as
configured.

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
- `PATCH /flights/trackers/<id>`: optional `budget`.
- `GET /flights/airports?q=...`.
- `GET /notifications?user_id=...`, `POST /notifications/<id>/retry`.
- `GET /analytics/delivery`.

The dashboard language selector persists in browser storage and is independent
of Discord profile language. API identifiers and stored user content remain
unchanged.

For future recurring occurrences, a nonexistent local clock time is skipped;
an overlapping clock time uses its first occurrence.
