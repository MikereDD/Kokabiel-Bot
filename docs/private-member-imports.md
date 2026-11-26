# Private Member Imports

## Authorization

- Owners retain existing private-chat access.
- Ordinary members initiate `/import` in a group explicitly
  listed in `ALLOWED_CHAT_IDS`.
- Empty `ALLOWED_CHAT_IDS` does not authorize member imports.
- Telegram membership must be successfully verified.
- Group administrator status does not grant Kokabiel owner privileges.
- Unknown users, anonymous senders, bots, departed members,
  and unverifiable memberships are denied.

## Private workflow

1. Request `/import` from the authorized group.
2. Continue in the DM initiated by Kokabiel.
3. If needed, use the private activation link.
4. Upload a supported native tracker export.
5. Review the import preview.
6. Explicitly approve the full or verified-only import.

Successful DM delivery requires no group response.

Telegram may prevent a bot from initiating a private conversation.
The activation link expires after ten minutes, is single-use,
and is bound to the requesting user and originating group.

If private activation delivery is unavailable, no public
fallback containing personal import information is attempted.

Member sessions expire after 24 hours. Expiration is checked
when the session is accessed. Restarting the bot invalidates
its in-memory import and activation sessions.

An active database transaction is drained safely rather than
interrupted by session expiration.

## Clearing imports

Cancel Import discards an unfinished session.

For completed imports:

1. Open a new authorized private import session.
2. Select Import History.
3. Select one of your recorded import batches.
4. Review the eligible and retained title counts.
5. Confirm Undo or cancel.

History displays open journaled batches in pages of eight.

Back to Upload returns to document intake without granting
additional access or extending the existing session lifetime.

## Transaction safeguards

Each successful import records personal before-and-after images
in the same transaction as the imported records.

Subsequent changes to a title invalidate automatic undo for
that title, including changes made by later imports.

Undo restores only eligible personal records. It does not
delete shared catalog metadata, group associations, suggestions,
release metadata, or another user's personal records.

A successful partial undo closes the selected batch.
Skipped titles are retained.

Stale approvals are rejected. Imports and undo operations
require verified backups and run through the supervised
import writer.

## Privacy and limitations

Import interactions occur privately. Existing group watchlist
and aggregate-rating features continue to operate normally.

Personal tracking records remain global per Telegram user.
Group associations determine participation in existing group
aggregates; importing privately is not a separate hidden
media library.

Telegram only guarantees membership lookups for other users
when the bot is a group administrator. Member imports fail
closed when current membership cannot be verified.

Imports completed before journal support have no reliable
provenance and cannot be automatically reversed.

Undo is not a complete account-data deletion mechanism.
Journal records and database backups may retain historical
personal values. No automatic journal or backup retention
policy is introduced by this feature.

This feature does not grant general private-command access
to ordinary members.
