# Dogginator MediaBot Architecture

## Primary Rule

Discord commands DO NOT own provider-specific HTTP behavior.

Correct:

Discord Command
    |
    v
Service Layer
    |
    v
Provider

Wrong:

Discord Command
    |
    v
random HTTP calls scattered everywhere


## Authorities

### Seerr

Authority for:

- movie search
- television search
- movie requests
- television requests
- request state
- TMDB metadata
- discovery metadata


### Jellyfin

Authority for:

- actual available library
- Jellyfin item IDs
- watch URLs
- watched / unwatched state
- play history
- recently added media
- user playback data


### Sonarr

Authority for:

- the exact episode catalog of series already known to Sonarr
- monitored episodes
- episode search, queue, and download state
- partial-season repair

Seerr remains the approval/request broker for new media and empty/unapproved
seasons. For a genuinely partial season whose Seerr state proves it was already
approved, MediaBot uses exact Sonarr episode IDs rather than pretending Seerr's
coarse `Partially Available` flag can re-request the season. Jellyfin is still
the final authority for whether a physical episode is indexed and playable.


### SoulSync

Authority for:

- music discovery
- music acquisition
- artist watchlists
- music recommendations
- music automation


### MediaBot

Authority for:

- Discord UX
- Discord -> Seerr/Jellyfin identity mapping
- orchestration
- request-message lifecycle
- durable five-minute transient-interface cleanup
- exact Jellyfin playback-problem reports and per-guild admin queues
- votes
- events
- notifications
- cross-provider reconciliation
- durable per-Discord-user 1-10 ratings
- recommendation scoring and explanations


### Trakt Plugin

Optional enrichment for the configured owner profile:

- sync explicit 1-10 MediaBot ratings for titles already in Jellyfin
- contribute Trakt recommendation candidates
- never replace the local rating store
- never receive ratings from arbitrary household Discord users


## v0.5 Taste Pipeline

Local MediaBot ratings + Trakt 1-10 ratings + Jellyfin history
    |
    v
explicit-rating/favorite affinity and already-seen exclusions
    |
    +-- rank-decaying Trakt recommendation boost
    |
    v
Seerr/TMDB requestable discovery pool
    |
    v
$recommend ranked cards with request buttons

`$recommend` ranks requestable Seerr/TMDB titles using the taste pipeline
above. `$discover` is deliberately separate: it ranks only movies and series
already present in Jellyfin and returns direct Watch in Jellyfin cards without
entering the request workflow. Its default ranking is community rating then
library recency. `$discover --random` and `$recommend --random` sample only
inside their configured top-ranked pools.

`$discover` and `$recommend` default to a combined movie/show pool when no
media type is supplied. Multiple genres use an AND intersection, including the
human-friendly `love` alias for `Romance`; `$recommend` applies this upstream
to TMDB while `$discover` applies it to the current Jellyfin inventory.

TMDB does not define a native Romance genre for TV. MediaBot therefore treats
TV Romance as a semantic genre backed by TMDB's `romance`, `love`, and
`courtship` keywords. A title such as Bridgerton remains a TMDB Drama while
also satisfying MediaBot's TV Romance filter. Mixed `$recommend` batches with
two or more results reserve space for both movies and TV when both pools have
eligible titles; a four-card batch is therefore two movies and two shows.

Watched/played history is exclusion-only and never implies that the user liked
a title. Explicit local or Trakt 1-10 ratings create positive and negative
genre affinity, while Jellyfin favorites/likes remain a positive signal. When
the same rating exists locally and on Trakt, the local row overrides the Trakt
copy so the preference is not counted twice. Trakt recommendation order is
preserved and its score boost decays by rank rather than applying a flat match
bonus.

Genre expressions support implicit AND, explicit `&&`/`and`, `||`/`or`,
commas as AND, and parentheses. Expressions are compiled to disjunctive
normal form. Recommendation AND branches become exact TMDB comma queries;
available discovery evaluates the same branches against Jellyfin genres and
uses the TV Romance keyword fallback only where Jellyfin metadata is ambiguous.

Multi-card recommendation replies share live batch state and persist their
terminal state in SQLite. Dismissing a card deletes that bot message
immediately. If every card reaches the dismissed or expired state, MediaBot
also deletes the originating Discord command; any kept or requested card
preserves the command for context. Every unfinished public search, selection,
or confirmation card expires after five minutes. A lease-based cleanup worker
continues that policy across process and host restarts without racing a user
who accepts at the deadline.


## Public Command Model

The normal user surface is deliberately small:

- `$request <title>` requests a known movie or show.
- `$music <artist and track>` requests a known song.
- `$discover` browses media playable in Jellyfin now.
- `$recommend` ranks unseen, requestable media from taste signals.
- `$rate <title> <1-10>` pages through exact matches before writing a rating;
  `$rate` with no title lists existing ratings.
- `$status <title or #request>` reconciles video, episode, and music state.
- `$report <library title [SxxExx]>` files an exact playback-problem ticket.
- `$event` creates, nominates, votes, schedules, and shows event-night media.
- `$new` shows recent additions.
- `$help` explains this model before exposing utilities.

`$random` is a compatibility alias for `$discover --random`.
`$randomrequest` and `$rr` are compatibility aliases for
`$recommend --random`. `$ratings` is a compatibility alias for `$rate` with no
arguments. They are not separate product concepts and do not appear as
top-level help commands.


## Target Package Layout

mediabot/
|
|-- bot.py
|
|-- core/
|   |-- config.py
|   |-- database.py
|   `-- logging.py
|
|-- providers/
|   |-- base.py
|   |-- seerr.py
|   |-- jellyfin.py
|   |-- sonarr.py
|   `-- soulsync.py
|
|-- services/
|   |-- requests.py
|   |-- library.py
|   |-- discovery.py
|   |-- recommendations.py
|   |-- notifications.py
|   `-- events.py
|
|-- commands/
|   |-- help.py
|   |-- request.py
|   |-- status.py
|   |-- discover.py
|   |-- recommend.py
|   |-- music.py
|   |-- report.py
|   |-- event.py
|   |-- spooktober.py
|   `-- admin.py
|
`-- ui/
    |-- media.py
    |-- search.py
    |-- pagination.py
    `-- confirmation.py


## Request Lifecycle

Discord
  |
  | $request Alien
  v
MediaBot
  |
  v
Seerr Search
  |
  v
Discord Search Card
  |
  v
User selects media
  |
  v
Seerr Request
  |
  v
MediaBot stores:
  - Seerr Request ID
  - TMDB ID
  - Discord Message ID
  - Discord Channel ID
  - requester
  |
  v
Radarr / Sonarr
  |
  v
download
  |
  v
Jellyfin scan
  |
  v
MediaBot resolves TMDB ID -> Jellyfin Item ID
  |
  v
ORIGINAL DISCORD CARD EDITED
  |
  v
WATCH IN JELLYFIN


## Playback Report Lifecycle

`$report` searches Jellyfin only: a playback report cannot target media that
is not actually playable in the library. Series episode syntax accepts
`S02E07`, `2x07`, or plain `season 2 episode 7`, and resolves the exact
Jellyfin episode item before a ticket can be submitted.

The user chooses one of six categories: Won't Play, Wrong Audio, Bad
Subtitles, Bad Quality, Wrong Episode, or Other. Other requires a short
description. SQLite deduplicates active reports for the same guild, reporter,
target, and category.

Administrators use `$admin reports` to page through the guild-local queue and
claim, resolve, or dismiss a ticket. Every read and state transition includes
the Discord guild ID so an administrator in one server cannot access another
server's queue. MediaBot identifies the exact item/file but deliberately does
not automate replacement or deletion: playback remediation can be destructive
and remains an administrator decision.


## v0.8 Event Lifecycle

Events are generic, guild-scoped ballots rather than a Spooktober-specific
table or command family:

```text
OPEN -> SCHEDULED -> COMPLETED
  `----------------> CANCELLED
```

Each guild may have one open ballot while retaining any number of older
scheduled events. Users nominate exact Seerr/TMDB movie or TV results and vote
within the event's saved limit. Ranking is deterministic: vote count descending,
then nomination creation order. Scheduling is a revision-checked transaction;
if a nomination or vote changes after preview, confirmation fails and the
organizer must review a fresh plan rather than freezing stale results.

Generic schedules accept comma-separated local `YYYY-MM-DD HH:MM` values in
the event timezone. The parser rejects malformed, duplicate, nonexistent DST,
and ambiguous DST times. Storage is UTC; `$event tonight` converts a local
calendar-day boundary into UTC and reads all matching scheduled slots.

Spooktober is a versioned preset on the same engine. Creation snapshots its
movie-only Horror rule and exact October slot timestamps into the event row,
so a later bot upgrade cannot mutate an active year's ballot. Empty preset
slots remain explicit TBD entries when fewer titles are nominated than nights.

v0.8 intentionally does not auto-request winners, post reminders, replace
Discord calendar tooling, or retain persistent component callbacks across a
restart. Successful vote/schedule receipts remain static; unfinished controls
follow the durable five-minute cleanup lifecycle.


### Existing-series episode lifecycle

For a series already present in Sonarr, the season selector is built from
Sonarr/TVDB's exact episode rows rather than TMDB's sometimes incomplete season
catalog. It distinguishes:

- complete: every known episode has a file;
- partial: at least one episode exists and specific gaps remain;
- empty: every known episode is missing;
- queued: remaining gaps are in Sonarr, with warning/import states preserved;
- upcoming: announced episodes have not aired yet and may already be monitored;
- unreleased: no episode rows exist yet.

The selector starts with nothing selected. Empty/new season requests still go
through Seerr so user permissions, approvals, and quotas remain authoritative.
Selecting a genuine already-approved partial repair monitors only the exact
missing episode IDs in Sonarr; MediaBot never silently monitors the whole
season. A search command is issued only for aired selected episodes. The
MediaBot availability watcher snapshots only episode numbers Sonarr actually
accepted and does not mark the Discord card complete until Jellyfin reports
those physical episode numbers.


## Request Providers

Video:

$request movie
$request tv

-> Seerr

Existing series / approved partial-season repair:

`$request <series>` -> Sonarr exact episode monitor/search -> Jellyfin


Music:

$music <artist and track>
$status <movie, show, or music track>

-> SoulSync

MediaBot uses SoulSync's public v1 search and inbound-request API. It owns
Discord selection, confirmation, cancellation, and expiry cleanup; SoulSync
owns source matching, acquisition, organization, and the media-server scan.
Music request state is persisted by MediaBot and reconciled into the common
`$status` command. There is no separate music-status command or duplicate song
alias.

Album acquisition and artist watchlists remain later SoulSync expansions. The
public request endpoint currently accepts one search query and does not expose
an atomic album-request contract, so MediaBot does not pretend that a track
match is a whole album.


MediaBot MUST NOT become a replacement for:

- Seerr
- Jellyfin
- Radarr
- Sonarr
- SoulSync

It orchestrates them.
