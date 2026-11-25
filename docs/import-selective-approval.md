# Selective Tracker Import Approval

## Status

Milestones M1-M4 — implementation and development validation.

Audit remediation branch: `fix/import-audit-remediation`

Remediation baseline: `740d78b9141abc80bfb0aab1a1e1c67db4882d40`

Supported runtime: ARAKIEL, Arch Linux ARM.

Final independent review and live acceptance remain outstanding.

Validation environment: ARAKIEL, Python 3.14, disposable SQLite
databases and simulated Telegram interactions.

## Purpose

Allow users to import verified tracker records without requiring
every unresolved or conflicting record in an export to be resolved.

Selective approval does not weaken identity resolution, database
validation, transactional safeguards or existing privacy requirements.

## Supported tracker sources

- Trakt
- IMDb
- Letterboxd
- Simkl

Supported native CSV, JSON and ZIP formats remain subject to the
existing source-specific detection and parsing rules.

### Simkl anime episode history

Native anime episode coordinates are retained as source data,
but are not assumed to match TMDB episode coordinates.

Unverified episode mappings require review. Selective approval
excludes the associated dependent group while retaining
unrelated eligible records.

Anime movies and anime series without explicit episode history
retain their normal import behavior.

## User workflow

1. Run `/import` in an authorized private chat with Kokabiel.
2. Upload a supported native tracker export.
3. Review the initial import preview.
4. If every record is verified, use the ordinary Import action.
5. If verified changes coexist with excluded records, choose
   Prepare Verified Import.
6. Review the independently recalculated eligible subset.
7. Choose Import Verified Records to authorize that exact subset.
8. Receive the transaction result when delivery succeeds.

Cancellation remains available while awaiting an approval decision.

An export containing no eligible changes cannot offer selective
application.

## Dependency exclusion

Records requiring review are excluded together with records linked
through their identity dependencies.

Dependency grouping considers:

- Canonical TMDB identities.
- Secondary tracker identifiers.
- Conflicting identifier claims.
- Possible resolution candidates.
- Normalized title relationships.
- Series and associated episode identities.

Movie and television identity namespaces remain separate where
the source identity permits that distinction.

Title relationships establish conservative exclusion dependencies.
They do not independently authorize identity resolution.

## Preview architecture

The full preview and eligible subset are initially calculated from
the same read-only database snapshot.

The eligible subset is independently replanned rather than reusing
database actions calculated for the full preview.

Source-record accounting must remain consistent across eligible
and excluded records.

Preparation refreshes the preview against current database state.
A changed or unavailable preview fails closed.

## Authorization

Initial preview and verified confirmation tokens are distinct.

The initial token cannot authorize selective application.
The verified confirmation token cannot authorize ordinary application.

Authorization is bound to the originating user, private chat,
session state and exact approved preview.

Approval is unavailable until the corresponding preview message
has been successfully delivered.

Consumed, cancelled, stale and replaced authorizations cannot
be reused through the Telegram callback workflow.

## Session lifecycle

- awaiting_upload
- processing
- ready
- preparing_verified
- verified_ready
- applying

Active processing, preparation and application cannot be replaced
by another import command or interrupted by cancellation buttons.

Group import commands cannot discard existing private sessions.

Concurrent document submissions are guarded against duplicate
processing and stale asynchronous authorization.

## Transactional safeguards

Selective imports use the existing transactional database writer.

- Review-required previews are rejected.
- Current database state is validated before application.
- Existing conflicting ratings are protected.
- Episode history remains union-only.
- Excluded records and linked dependencies are not applied.
- Stale approvals and semantic no-ops do not create new backups.
- Mutating imports require a verified pre-import database backup.
- Backup destinations are reserved exclusively.
- Backup creation has a cooperative execution deadline.
- Final logical import state is checked before transaction commit.
- Transaction failures roll back import changes.
- Backups are retained when required for recovery.
- Reapplication is protected against duplicate changes.
- A semantic no-op does not prove that a previous import occurred.

Pre-import backups contain the complete SQLite database,
including other users' data. They must remain private.

No database schema changes or new dependencies are required.

## Privacy and quarantine

Tracker documents are accepted only in private import sessions.

Raw exports are stored in the existing quarantine structure with
owner-only access protections.

Raw-file deletion is attempted immediately after preview construction
and during failure cleanup.

Group documents are not ingested as personal tracker imports.

## Communication failure recovery

Failed initial preview or confirmation delivery invalidates the
associated authorization.

Failed application acknowledgement prevents the transactional
writer from starting.

Failure to deliver a completion message after a committed transaction
does not undo the transaction or initiate another application.

## Cooperative cancellation

Cancellation during document processing or preview preparation
invalidates the interrupted session.

An already-running parser worker is allowed to finish before its
quarantine file is removed.

An already-running database worker is allowed to finish before
the applying session is released.

The resulting transaction outcome is preserved. Cancellation does
not trigger an automatic transaction retry.

## Responsiveness and shutdown

Ordinary Telegram update dispatch remains sequential.

Import processing uses supervised background tasks, with a
maximum of two active imports and one import writer.

Informational commands can remain responsive during an import
transaction. Their activity records may be deferred in a
bounded queue of up to 32 entries.

Consistency-dependent actions can temporarily return a busy
response while a writer is active.

Accepted deferred activity is processed before the writer
reservation is released. Storage failures remain best-effort.

Configuration reload is restricted during active imports.
Graceful restart and shutdown wait for supervised work to
finish before releasing application resources.

## Validation

Final automated validation on ARAKIEL:

| Suite | Passed |
| --- | ---: |
| M4.5 lifecycle regression | 39/39 |
| M4.6 targeted regression | 49/49 |
| M4.6 full repository regression | 320/320 |

Prior independent harness revalidation included 2,500 randomized
iterations, 231 successful SQLite applications, and 231
successful idempotent replays.

This revalidation is not a fresh independent audit.

The smaller suites are included in the full repository suite and
must not be added to the full test count.

Additional validation:

- Python compilation passed.
- Tracked-file whitespace validation passed.
- Real SQLite selective transactions passed.
- Pre-import backup verification passed.
- SQLite integrity and foreign-key validation passed.
- Stale preview rejection passed.
- Callback replay protection passed.
- Simulated Telegram timeout recovery passed.
- Cooperative cancellation during an active transaction passed.

## Known limitations

- Live Telegram interaction and live TMDB communication have not
  been validated as part of this milestone.
- Handler-level integration tests use simulated Telegram transport.
- Forced process termination and power-loss recovery were not tested.
- Graceful shutdown during real disposable SQLite transactions
  passed automated application lifecycle integration tests.
- Actual production process replacement and OS signal delivery
  remain untested.
- Deferred activity has a bounded queue size, but no separate
  aggregate execution deadline.
- Historical backup retention requires an approved policy.
- Windows runtime compatibility is not supported.
- Quarantine deletion remains best-effort on filesystem errors.
- Selective approval does not provide interactive correction of
  excluded records.
- Conservative dependency grouping can exclude otherwise verified
  records that are linked to records requiring review.

## Release boundary

This milestone does not authorize deployment, production database
changes, service restarts, branch merges, tags or releases.

Live acceptance testing must be performed separately.
