# Daily recovery backups

The existing Pi timer saves the live SQLite database locally at 02:15. The separate
Windows task **Salon Max Daily Database Backup** runs at 02:45 and at sign-in. It
also catches up a missed scheduled run. Windows registered it in Interactive mode:
the PC must be on and the user signed in. The Pi and Tailscale connection must be
reachable. Failed runs retry three times, 20 minutes apart.

PC location: `C:\Users\benpa\SalonMaxBackups`.

* `backups/*.smbak`: encrypted, authenticated recovery archives; retain 30 daily
  groups. Pruning happens only after a complete, saved and verified backup.
* `backup.key`: recovery encryption key. Keep it with the backup installation and
  make a separate secure copy before replacing this PC. Losing this key makes the
  encrypted files unrecoverable. The folder is restricted to this Windows user,
  SYSTEM and local administrators.
* `status.json`: latest attempt, success/partial/error, time and saved filename.
* `schedule.json`: task schedule and actual Windows logon mode.
* `tools/backup.ps1`: installed worker, independent of the Codex workspace/runtime.

The collector streams the recovery archive over authenticated SSH. The PC encrypts
it with AES-256-CBC and a fresh random IV, authenticated using a separate HMAC-SHA256
key. No plaintext recovery ZIP is saved on the PC during normal backup. Saved bytes
are decrypted and every file hash checked before reporting success. The Pi uses
SQLite's online backup API and integrity_check; it does not copy an open DB file.

Contents include the complete live Pi database (customers, accounts, minutes,
transactions, staff/PIN hashes, settings, courses, stock and pending sync writes),
local platform/legacy databases, configuration/credentials, back-office source,
templates, uploaded assets, current native release source and updater state.

The hosted snapshot includes **every table in public and salon_config**, including
online login hashes, orders, customer minute/balance ledgers, settings and account
links. It uses one read-only repeatable-read transaction and includes table types,
defaults, constraints, indexes and sequences. The endpoint is software-manager-only,
HTTPS, with no-store response headers. This is an application database backup,
not a PostgreSQL server/role/extension dump or an OS image.

A cloud outage still saves the Pi archive but reports **partial**, returns failure
to Task Scheduler for retry, and does not prune older complete backups. A Pi outage
reports failure and preserves existing archives. The Pi and hosted snapshots have
separate capture times; restoring both does not itself resolve pending sync conflicts.

## Verify or recover

Use Windows PowerShell:

```powershell
& "$env:USERPROFILE\SalonMaxBackups\tools\backup.ps1" -VerifyLatest
& "$env:USERPROFILE\SalonMaxBackups\tools\backup.ps1" -ExtractLatestTo "$env:USERPROFILE\SalonMaxBackups\recovery-new"
```

The extraction destination must not exist. It contains plaintext sensitive data;
keep it in this restricted folder. Never overwrite the live till while services
are running. Restore the extracted `databases/till_backoffice.db` onto a replacement
Pi with all Salon Max services stopped, then check integrity, customer counts and
minute/account totals before reconnecting sync. Pending events and cloud ledger
cursors are part of the saved DB; do not reset them or re-credit balances manually.

For the hosted database, provision a NEW EMPTY PostgreSQL database and install the
same code/dependencies. Run the copied `restore_database_backup.py` against that
empty database **before starting the application**:

```powershell
# Set RESTORE_DATABASE_URL privately to the new empty database connection URL.
python restore_database_backup.py recovery-new/cloud/application-database.json --database-url-env RESTORE_DATABASE_URL
```

The restore tool refuses existing application tables/sequences. DDL, records,
constraints, indexes and sequences restore in one transaction, with row counts
verified. A failure rolls back the restore. PostgreSQL roles, grants, provider
configuration and external email/payment secrets still require separate setup.

## Tested

* Customer account balances, active/archive status, course minutes and pending
  writes restored correctly from a SQLite backup.
* Full application export restored into a separate empty PostgreSQL database,
  including a known 30-minute ledger credit; rerunning against it was refused.
* Encryption round-trip, random IV, damaged archive rejection and wrong-key
  rejection passed using the installed Windows PowerShell runtime.
* A hosted outage produced a preserved Pi backup with partial status.

PC-only protection depends on this PC and its key surviving. Daily backups can
lose changes made since the last successful copy (normally up to 24 hours).
They do not replace live sync, and do not guarantee zero loss during an outage.
