# Media Library and Release Intelligence

## Personal library

The personal library combines existing personal media state
and ratings. Titles are identified by media type and TMDB ID.

The library provides category, collection, status and sorting
controls with eight titles per page.

Viewing progress is grouped in this order:

1. Films
2. Television series
3. Documentaries

Use Released episode totals to retrieve current watched-versus-released
episode counts for series on the visible progress page.

Successful watched and watchlist actions return to the refreshed
library page with the original filters and sorting. Previous action
buttons expire before mutation, preventing accidental replay.

Recently Updated and Recently Added compare chronological timestamp
values without rewriting stored personal records.

## Release information

Film and documentary film cards display original release dates
and available regional theatrical, digital and physical releases.

Digital release information does not establish availability on
a particular streaming service.

Series information distinguishes continuing, ended, canceled,
planned, in-production, pilot and unknown lifecycle states.

Missing upcoming episode information does not establish that a
series has ended.

Release dates originate from TMDB and may change.

## Personal release schedule

Open `/myreleases` or select Release Schedule from the library.

The schedule includes:

- Watchlisted titles.
- Television and documentary series marked watching or caught up.
- Upcoming events within the selected 7, 30 or 90-day window.

Completed series without an active watchlist entry are excluded.
The schedule contains listed upcoming events, not a complete
future episode guide.

A season premiere and its first episode may appear separately.

## Metadata refresh

Refresh one batch checks up to 20 eligible titles.

Refresh all tracked titles processes successive batches in one
user-requested background operation, reporting progress after
each batch. Only outdated or unchecked metadata is eligible.

A refresh-all operation starts at most 50 batches and stops starting
additional batches after a 600-second overall start budget.
The final batch and message delivery may finish afterward.

Each title is attempted at most once per refresh-all operation.
Complete coverage depends on metadata availability, request latency,
rate limits and the operation's safety budgets.

The persistent cache is scoped by media type, TMDB ID and region.
Metadata becomes outdated after 24 hours.

Future-dated cache records are considered outdated. Recovery after
clock correction is conditional on the previously observed timestamp
still matching the stored timestamp.

Refresh requests are sequential within one active batch.
The worker stops starting additional requests after its
30-second start budget, although an in-flight request may
finish afterward.

Failed requests retain existing cached information and enter
a five-minute in-memory retry cooldown.

Previously failed titles are prioritized behind other stale titles
and rotate according to retry deadlines. Retry history and global
pause state reset when the worker is recreated, including restart
or TMDB client replacement.

Authorization failures and rate limiting temporarily pause
release refresh operations.

Shutdown stops refresh-all from starting additional batches.
An active batch may finish before shutdown completes.
A failed progress-message edit does not interrupt metadata collection.

The schedule identifies unchecked titles and outdated metadata.
An empty schedule with incomplete coverage does not establish
that no upcoming releases exist.

Reload schedule rereads the database without contacting TMDB.

Refresh is user-initiated. No automatic polling or unsolicited
release notifications are enabled.

## Database compatibility

Initialization adds the release_cache table to the existing
SQLite database.

Release metadata is separate from personal ratings, watchlists,
viewing status and episode history.

Import application and release refresh cannot overlap.
Configuration reload is unavailable during an active refresh.

Create a verified database backup before deploying an upgrade.

## Validation

The automated development suite covers library pagination,
personal identity isolation, release metadata, persistent caching,
refresh failure recovery, navigation and import coordination.

Migration has also been validated against an isolated copy of
an existing Kokabiel database.

Live Telegram delivery and actual TMDB metadata coverage require
deployment acceptance testing.
