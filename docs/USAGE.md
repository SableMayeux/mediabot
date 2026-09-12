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

1. Make sure the member exists in **Seerr**, not just Jellyfin. Have the member
   sign into the Seerr website with their media-server account, if new media
   sign-ins are enabled. Alternatively, the Seerr administrator opens its
   **Users** page and uses **Import Jellyfin Users** to import the intended
   account. Review the import selection and resulting permissions in your
   Seerr version. See [Seerr's account import and first-login instructions](https://docs.seerr.dev/using-seerr/users/adding-users/).
2. The Discord application's **bot owner**, who must also be a **server
   administrator**, runs `$admin users` in the allowed server or the bot's DM.
   The bot sends a private list of Seerr users and numeric IDs, split into pages
   when necessary. If the owner administers more than one allowed server, first
   select the DM context with `$admin server <server ID>`.
3. That same owner runs `$admin link @member 12`, replacing `@member` with the
   actual Discord mention and `12` with the intended Seerr user ID. Verify the
   match before linking. The Discord member's numeric ID also works, which is
   useful in DMs. Use the numeric **Seerr** ID from `$admin users`, not the
   Jellyfin user ID. A matching Seerr/Jellyfin username is also accepted, but
   numeric IDs avoid ambiguous names.
4. The member runs `$whoami` in the server. The bot privately confirms the
   linked Seerr identity.
5. The member sends `$request Interstellar 2014`, selects the exact result,
   and confirms the request. For TV, choose the desired seasons.
6. Keep the request receipt. `$status #123`, using its request number, checks
   progress. `$status Interstellar 2014` also works.

The bot owner is the Discord application's owner, not automatically everyone
with the server's Administrator role. A Jellyfin account, Seerr account and
Discord account are separate identities until the operator explicitly maps
them. `$admin link` creates that Discord-to-Seerr mapping. It does not import
Jellyfin users, create new accounts or change Seerr request permissions. If a
name is missing from `$admin users`, finish the Seerr import or first login,
then run the list command again. The operator controls new sign-ins and request
permissions in [Seerr's user settings](https://docs.seerr.dev/using-seerr/settings/users/).

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
| `$admin` tools | Administrators; sensitive linking/diagnostics also require bot ownership | Same current-server permission requirements; select a server if needed |

The different help lists reflect current permissions. A media account link does
not enable media commands in DMs. Buttons usually belong to the person who
opened the card, so start your own command instead of using someone else's
controls. Abandoned interactive cards expire after about five minutes; run the
command again. Successful receipts and event dashboards remain.

## Private administration

DM the bot with `$help admin` or `$admin` to see the administration commands
available to you. A current administrator of an allowlisted server can use
the report queue from there. User listing, account linking, integration health,
logs and errors also require ownership of the Discord bot application.

If you administer one eligible server, MediaBot uses it automatically.
`$admin server` lists the servers where you currently have access. If you
administer several, select the target before running an admin command:

```text
$admin server 123456789012345678
$admin users
$admin link 234567890123456789 12
$admin integrations
```

The first ID is the Discord server, the second is a Discord member and `12`
is the member's Seerr ID. Replace all three with real IDs. Server membership
and administrator permission are checked again when commands run; choosing a
server does not grant new access. Selection lasts until the bot restarts or
you select another server. If you lose access to the selected server, commands
are denied until you explicitly choose another authorized server. MediaBot does
not silently switch targets. The selected server determines the member
lookup and report queue. The installation still shares one Seerr provider and
account-link store across its trusted servers.

Commands continue to work in the server as before. DM administration does not
enable `$request`, `$status`, `$whoami`, media discovery, events or torrent
commands in DMs. Use the server channel for those commands.

## Optional local AI and Life

`$ask Explain the difference between HDR10 and Dolby Vision` answers publicly
in the channel where you ask. `$ask --private <question>` first copies your
question into a DM, then removes the guild command before inference. If DMs or
source deletion fail, inference stops. Asking directly in DM keeps the exchange
there. Public questions and answers remain visible in Discord.

The local model sees the question and that conversation's limited temporary
history. It cannot read your notes, browse channels, execute commands or change
tasks. Follow-up controls retain the previous messages.
Context expires after ten minutes; Discord's messages do not. This is not
persistent model memory.

With an updated conversation gateway, answers have room for explanations and
examples, up to 1,024 generated tokens. The footer identifies the model actually
used and indicates when generation hit its limit. Long replies retain the full
answer instead of silently cutting it off; continuation replies are used when
the answer and question exceed Discord's embed limit. The original question
stays visible. A legacy gateway still works, with its older limits identified.

For current prices, sales or news, explicitly enable web search:

```text
$ask --web What Nintendo eShop deals are available in the US today?
$ask --desktop --web What Nintendo eShop deals are available in the US today?
$ask --web --private Compare these current offers
```

Put flags before the question, in any order. Queries are limited to 800
UTF-8 bytes. The bot retrieves up to three sources, then gives their bounded
text to the local model. The answer has a source card with original links,
retrieval timestamps and labels identifying search excerpts when full pages
could not be read. Citation markers such as `[S1]` correspond to those cards.
If retrieval fails, the bot says so and does not generate an unsourced answer.
Retrieved text is untrusted evidence and cannot trigger homelab actions.

Follow-ups retain GPU selection and web mode and search the new question again. Only the current
question goes to search providers, so repeat the topic in questions such as
"Which of those Nintendo deals is cheapest?" Previous conversation messages
remain local to the configured model gateway. `--private` controls Discord
delivery; it does not keep a web search query private from upstream providers.
Use **Cancel generation** to stop either search or model generation. **New
topic** clears temporary context; start a fresh `$ask` to change GPU selection or web mode.

Use `$ask --desktop <question>` to require the desktop model with no server
fallback, or `$ask --server <question>` to use only the server. These flags are
mutually exclusive and combine with `--web` and `--private`. They retain your
existing permissions and do not turn the desktop on. An unavailable required
desktop produces an explanation without making a server request.

The reply footer identifies the selected mode and actual model and GPU. With
neither GPU flag, when the desktop is on, ready and permitted for your account, new
conversations and follow-ups prefer it. Turning it off or sleeping the desktop
allows new automatic requests to use the server only if you have server access;
the footer explains the fallback. An
interrupted generation is not silently retried on another GPU. The desktop
on/off switch does not change anybody's permissions. Automated task
classification and recommendation ranking retain their separate bounded
server profile and existing checks. `$recommend --auto` requires server AI
permission; ordinary `$recommend` still works without it. A late revocation
keeps the standard provider ranking and discards the model's ranking.

### Owner controls for AI access

The bot owner can run the same commands in the configured server or in DM:

```text
$admin ai status
$admin ai access @member
$admin ai allow @member desktop
$admin ai deny @member web
$admin ai reset @member web
```

Use a Discord user ID in place of the mention in DMs. Each user has independent
`server`, `desktop` and `web` permissions. The defaults allow current server
members to use server AI and web search; desktop AI starts owner-only. An
explicit allow or deny persists across bot restarts and desktop switching.
`reset` removes that override. To make someone desktop-only, allow `desktop`
and deny `server`; while the desktop is unavailable, that person receives an
availability message rather than falling back to an unpermitted model.

Current membership remains required, and each follow-up checks permission
again. The owner always retains access. AI grants apply across this bot's
configured servers and do not grant Life storage, media administration or
access to another person's conversation. `$admin ai status` reports backend
readiness separately from permissions. Use the DesktopAI shortcuts on the
desktop to operate its switch.

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
