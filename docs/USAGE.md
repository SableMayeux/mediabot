# Use MediaBot

MediaBot gives your Discord server one place to ask for media, find something
to watch and check what happened to a request. You talk to it with `$` commands,
then use the result cards, dropdowns and buttons. Playback happens in your media
app, and download approval stays with your server's configured services.

Start in the server's bot channel with `$help`. It lists the commands available
to your account in that location. `$help request` explains a specific command.
Examples below use the default `$` prefix.

## Your first request

The installer gets the bot online. This one-time account setup makes requests
belong to the right person:

1. The member signs into the server's **Seerr** website. The server operator
   controls which Jellyfin, Emby, Plex or local accounts can sign in and what
   they can request. See [Seerr's user settings](https://docs.seerr.dev/using-seerr/settings/users/).
2. The Discord application's **bot owner**, who must also be a **server
   administrator**, runs `$admin users` in the allowed server. The bot sends
   that owner a private list of Seerr users and numeric IDs.
3. That same owner runs `$admin link @member 12`, replacing `@member` with the
   actual Discord mention and `12` with the intended Seerr user ID. Verify the
   match before linking. Numeric IDs avoid ambiguous display names.
4. The member runs `$whoami` in the server. The bot privately confirms the
   linked Seerr identity.
5. The member sends `$request Interstellar 2014`, selects the exact result,
   and confirms the request. For TV, choose the desired seasons.
6. Keep the request receipt. `$status #123`, using its request number, checks
   progress. `$status Interstellar 2014` also works.

The bot owner is the Discord application's owner, not automatically everyone
with the server's Administrator role. A Jellyfin account, Seerr account and
Discord account are separate identities until the operator explicitly maps
them. Linking does not install Seerr or create a media-server account.

The expected outcome is a durable receipt showing an accepted request or a
clear existing/available state. **Accepted is not downloaded.** Seerr approval,
Radarr/Sonarr acquisition and Jellyfin library import are separate stages.
When a tracked request becomes available, MediaBot replies to its completed
request card and mentions its requester. If it needs approval, follow the
server's approval process rather than submitting the same title repeatedly.

## Find something to watch

| What you want | Try this |
| --- | --- |
| Browse comedies already playable in Jellyfin | `$discover movie Comedy` |
| Pick randomly from the playable library | `$discover --random --count 3` |
| Find something new to request | `$recommend movie --count 3` |
| Narrow new suggestions by genre | `$recommend (Fantasy and Romance) or Action --count 3` |
| Record your opinion | `$rate Interstellar 9`, then select the exact result |
| Review your saved ratings | `$rate` |
| See recently added media | `$new 5` |

`$discover` is the watch-now path. `$recommend` is the find-and-request path.
Use their cards to inspect a title and take the offered action. Viewing a
recommendation does not submit a request by itself.

Ratings are stored by MediaBot. Optional Jellyfin history and a configured
Trakt taste profile can enrich recommendations, but linking your Seerr account
does not automatically create a personal Trakt integration. An installation's
configured owner taste profile is separate from your own ratings.

If the operator has installed a local AI gateway, `$recommend --auto --count 3`
lets that model reorder a small set of real provider candidates. It does not
invent titles or bypass filters. Invalid output or unavailable inference keeps
the normal ranking. This option is experimental and may not improve your picks.
`$discover` and its `$random` alias do not accept `--auto`.

## Music, playback problems and media nights

Use `$music artist and song title` to choose an exact track through SoulSync.
Check it with `$status artist and song title`. The music command needs a
configured compatible SoulSync service.

Use `$report Interstellar 2014` for a playback problem, or include an exact TV
episode such as `$report Example Show S01E03`. Select the correct Jellyfin item
and describe what failed. Administrators use `$admin reports` to claim and
resolve reports. A report does not silently delete or replace your media.

For a shared watch night, an administrator runs `$event create Friday movies`.
Members add exact titles with `$event nominate <title>`, then open `$event`
to vote for titles and the times they can attend. The administrator proposes
times and publishes the schedule with the dashboard controls. Scheduled-event
publishing depends on the bot's event permissions. `$event tonight` and
`$event history` show the lineup and previous events.

Date pickers display the event's configured timezone. Discord timestamps
display in each reader's local timezone. Administrators can reschedule or
reopen an event without recreating its nominations and votes.

## Server messages and DMs

| Command group | Server channel | Direct message to the bot |
| --- | --- | --- |
| `$help` | Yes, permission-aware command list | Yes, DM-specific command list |
| Requests, discovery, ratings, status, music, reports, events, `$whoami` | Yes, with the required provider/account permissions | Not currently supported |
| `$ask` | Current allowed-server members and bot owner, if AI is configured | Same membership check, if AI is configured |
| `$think`, `$capture`, `$life` | Bot owner, private workflow | Bot owner |
| `$torrent` intake and review | Linked members for intake; owner/admin for review, if its gateway is configured | Not currently supported |
| `$admin` tools | Administrators; sensitive linking/diagnostics also require bot ownership | Not currently supported |

The different help lists reflect current permissions. A media account link does
not enable media commands in DMs. Buttons usually belong to the person who
opened the card, so start your own command instead of using someone else's
controls. Abandoned interactive cards expire after about five minutes; run the
command again. Successful receipts and event dashboards remain.

## Optional local AI and Life

`$ask Explain the difference between HDR10 and Dolby Vision` answers publicly
in the channel where you ask. `$ask --private <question>` first copies your
question into a DM, then removes the guild command before inference. If DMs or
source deletion fail, inference stops. Asking directly in DM keeps the exchange
there. Public questions and answers remain visible in Discord.

The local model sees the question and that conversation's limited temporary
history. It cannot read your notes, browse channels, search the web, execute
commands or change tasks. Follow-up controls retain the previous messages.
Context expires after ten minutes; Discord's messages do not. This is not
persistent model memory.

Life is currently an **owner-only integration**, not a shared task service for
every member. With its separately provisioned gateway:

```text
$think Call the mechanic about the estimate
$life
$life tasks
$think --auto Call the mechanic about the estimate
```

Plain `$think` always saves the raw note first. `$life` lets the owner choose a
capture, prepare a task or calendar event and explicitly confirm it. Task due
dates and notification reminders are separate fields.

`$think --auto` saves the note, then attempts one task decision. It uses only a
title quoted from that capture. Ambiguity, unavailable services or invalid
output leaves the note for manual review. It does not infer dates or reminders.
A small model can still miss an obvious task; the saved capture remains the
reliable source. Use `$life` to promote it yourself when that happens.

The portable installation can keep raw owner captures in its data volume. Task
and calendar operations require the separate Life gateway and Nextcloud setup.
Home Assistant, voice devices and multi-user Life enrollment are not installed
by the MediaBot setup script.

## Optional torrent workflow

Torrent commands require an operator-provisioned VPN/quarantine gateway. The
portable installer does not install qBittorrent, a VPN, a scanner or that gateway.
MediaBot has no direct qBittorrent administrator credentials.

In the configured server, a linked member can submit:

```text
$torrent game magnet:?xt=urn:btih:...
```

Use the real magnet and the appropriate type: `movie`, `tv`, `music`, `game`,
`app` or `other`. The command removes its source message before submission;
permission failure stops intake. Games, applications and other manual payloads
wait for owner/admin approval. Select **Review privately**, or run
`$torrent review`, inspect the full manifest and selected bytes, then approve
the intended files. Cancelling review preserves payloads.

The review shows the current download location and scanner coverage. Manual
payloads stay in the gateway's configured manual quarantine; they are not
automatically installed, imported into Jellyfin or moved into a games library.
The path is deployment-specific. The operator must provide an actual SMB share,
file-browser URL or other access method and map the container path to it.

To establish completion, compare the full manifest, selected file sizes,
completed bytes and the files at that location. All files skipped can produce
zero selected bytes and a misleading completed state. `stalledDL` means the
download is waiting without transferring; `stalledUP` means it is waiting to
upload. Neither state by itself identifies missing data or proves a broken VPN.
Use the selected files and actual filesystem as the evidence.

Large files or archive contents may fall outside the configured scanner's
coverage. An approval, partial scan or seeding state is not a safe-execution
guarantee. Inspect the scan receipt before opening downloaded software, and
keep the files in their existing location while seeding.

For full command syntax and provider boundaries, see the
[command and integration reference](REFERENCE.md).
