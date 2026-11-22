# Sync audit — Ultra Violet 2 — 22 November 2026

This is a point-in-time audit, not certification that every setting is continuously
synchronised. Run `python3 ~/Salon-Max/sync_audit.py` on the paired Pi for a fresh,
read-only customer/course comparison. Its output contains account numbers and field
names, not customer contact information or authentication secrets.

## Live findings

| Area | Evidence and result |
| --- | --- |
| Customer population | SQLite has 2 active and 1 archived customer. Cloud profiles contain all 3; active cloud directory contains 2. The apparent missing customer is archived. |
| Customer profiles | First/last names, birthday, email, phone, postal address and active/archive status match for all 3 accounts. |
| Active customer money/minutes | Both active customers match the cloud directory: 25 and 101 minutes, £0 balances at the check. Archived balances were not included in this comparison. |
| Courses | All course codes and their names, minutes, prices, validity and active status match. |
| Pending writes | 0 unacknowledged transaction outbox rows; 0 pending profile outbox rows. This proves current queue acknowledgement, not completeness of every historical financial projection. |
| Database health | SQLite quick_check: ok. Foreign-key check: 0 violations. |
| Estate | Authenticated cloud inventory reports 1 active site, 1 terminal, 2 bed devices. This does not certify native sunbed configuration rows or multiple-site operation. |
| Cash sessions | Initially 9, later 10 open session rows for the same terminal. Signing in and opening the till repeatedly created additional cash sessions. No financial history was deleted or retrospectively closed. |

## Changes

* Pi dashboard active customer count and financial totals now use the same active
  filter as the customer list. Archived customer count is visible separately.
* Open Tills counts distinct terminal IDs. Additional open cash session rows trigger
  a visible review warning rather than being silently hidden or closed.
* Native `open_till` atomically resumes the newest open session for a terminal;
  concurrent calls cannot create duplicate sessions. A closed session allows a new
  one. An open session prevents silently moving its terminal to a different site.
* Correction replay ignores already-applied ledger IDs inside the same SQLite write
  transaction as the cursor update. An unknown account blocks cursor advancement
  past that entry, allowing retry after customer import. A supplied customer number
  cannot fall through to another account number or a person's name.
* Signed native release: `2026.11.22.4`. Installation uses the existing managed
  safe-idle/sign-out policy. Publishing is separate from verifying installation.

Back-office source/template backups are taken by `backoffice_consistency.py` before
editing the separately installed Pi back office. Native code and release artifacts
are version controlled. No live customer minutes were changed as test inputs.

## Gaps found in the code review

These must be resolved and tested before describing the entire system as one
continuously synchronised database:

* Staff/PIN records and hosted web login roles have separate lifecycles. They are
  not a universal bidirectional staff/settings feed.
* Pricing, stock and business settings have local/configuration or commissioning
  paths rather than the tested customer-profile/course bidirectional feed. Some
  commissioning values can overwrite local settings; draft profiles are excluded.
* Cross-terminal financial propagation is not proven. The correction feed covers
  management adjustments and web purchases, not every other terminal's sales.
  Cloud sale customer identities are derived from local database IDs; those IDs
  are not a sufficient business-wide identity for independently created databases.
* Directory import can overwrite an existing customer's financial totals while
  a separate correction feed also applies deltas. Offline writes, overlapping
  snapshots and out-of-order delivery need an explicit shared ledger/version
  strategy and multi-terminal tests before broadening this path.
* Directory incremental timestamps use customer updates, whereas separate minute
  summary updates do not always advance that timestamp. Timestamp ties need a
  stable cursor; a successful feed request alone is not proof of completeness.
* Native sync catches exceptions without surfacing them in the till UI. A visible
  last-success/pending/error state is needed before users can distinguish fresh
  data from an offline copy.
* Local sunbed settings and cloud bed inventory are distinct representations.
  Inventory agreement alone does not prove names, enabled state, timing and
  hardware configuration agree across separate Pi databases.

When connected, current profile/course timers run about every 15 seconds; the
native transaction sync runs while the app is running. An offline device cannot
be current immediately. Queued writes must survive disconnection, retry once, and
show their pending state. Never resolve a mismatch by blindly re-crediting minutes
or replacing a live balance from an older snapshot.

## Validation

* 4 new isolated SQLite tests: dashboard scope, concurrent session opening,
  session close/reopen/site isolation, correction replay/missing-account retry.
* 4 existing managed updater tests passed.
* 24 disposable-PostgreSQL integration tests passed; 3 recovery-snapshot tests
  intentionally excluded because the live encrypted recovery key is unavailable
  in the local test environment. Recovery restore was disabled for that run.
* Live dashboard and authenticated customer/course audit checked separately.
