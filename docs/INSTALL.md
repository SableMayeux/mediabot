# Install your own MediaBot

This guide installs a separate MediaBot for your Discord server and your media
stack. Start with Discord and Seerr, prove one request works, then add optional
providers. The installer asks for API credentials and URLs, never your SSH or
server login password.

## What you need

- A Linux host with Docker Engine and Docker Compose 2.20 or newer. Use the
  [official Docker installation instructions](https://docs.docker.com/engine/install/)
  for your distribution, including its Compose plugin. Run `docker version` and
  `docker compose version` as the account that will run MediaBot.
- Git and Python 3.10 or newer on that host. The setup script uses only Python's
  standard library. Docker supplies the bot's separate Python 3.13 runtime.
- A Discord server you administer and a new Discord bot application.
- A working Seerr installation, already connected to your media/download stack.
  Confirm that a request works in Seerr's own web interface first.
- Outbound connectivity to Discord, image/package registries during builds, and
  your configured providers. MediaBot does not publish an inbound listening port.

Use one installation, bot application, credential set and persistent database
per independently administered media stack. Several allowlisted Discord servers
can share one installation, but they share its provider credentials and account
links. That is a trusted shared stack, not isolation between unrelated customers.

## 1. Create and invite your Discord bot

In the [Discord Developer Portal](https://discord.com/developers/applications),
create an application. On its **Bot** page, obtain the bot token and enable
**Message Content Intent** and **Server Members Intent** under **Privileged
Gateway Intents**. MediaBot uses prefix commands and membership checks. Presence
intent is unnecessary. Larger applications may require Discord's intent access
review; follow any requirement displayed in the portal. See
[Discord's Gateway documentation](https://docs.discord.com/developers/events/gateway#privileged-intents).

Use **OAuth2 → URL Generator**, select the `bot` scope, and choose the permissions
below. Open the generated URL and install it into your server. These are text
commands and buttons, so slash-command registration is not part of setup.

| Bot permission | When to grant it |
| --- | --- |
| View Channels | Required in the channels where members use MediaBot. |
| Send Messages | Required for responses and receipts. |
| Embed Links | Required for the normal cards and result lists. |
| Read Message History | Required for revisiting tracked messages and dashboards. |
| Attach Files | Needed for file attachments such as private diagnostic or manifest output. |
| Manage Messages | Needed for command cleanup and source-message deletion in private capture, private chat and torrent workflows. Scope it to the bot channels. |
| Create Events, Manage Events | Optional, for publishing and maintaining native Discord Scheduled Events. |
| Send Messages in Threads | Optional, only if you want commands in existing threads. |

**Do not grant the bot Administrator.** The human running `$admin` commands must
have server Administrator permission; that is separate from the bot's role.
Channel overrides can deny permissions even when its role grants them. Start
with an ordinary text channel and check that channel's effective permissions.
Permission meanings are documented in
[Discord's permission reference](https://docs.discord.com/developers/topics/permissions).

In your Discord client, enable **User Settings → Advanced → Developer Mode**.
Right-click your server and choose **Copy Server ID**. This is the guild ID,
not a channel ID, application ID or client secret.

## 2. Gather provider settings

The wizard requests these connection settings. The first four are required;
the public URL prompt defaults to the internal URL. Change it if members use
a different address.

| Setting | Where it comes from |
| --- | --- |
| `DISCORD_TOKEN` | The application's **Bot** page. Use its bot token, not a user token or OAuth client secret. |
| `ALLOWED_GUILD_IDS` | Your copied server ID. Separate trusted server IDs with commas. |
| `SEERR_URL` | Base URL reachable from the MediaBot container, without `/api/v1`. |
| `SEERR_API_KEY` | Seerr **Settings → General → API Key**. |
| `SEERR_PUBLIC_URL` | The Seerr web address your Discord members can open. Blank uses the internal URL. |

Seerr's API key grants administrative access. Enter it into the local hidden
prompt, not Discord. Its location and application URL are described in
[Seerr's general settings](https://docs.seerr.dev/using-seerr/settings/general/).

Internal and public addresses solve different problems:

| Provider location | Example internal URL |
| --- | --- |
| Another LAN host | `http://192.168.1.20:5055` |
| Published port on the same Docker host | `http://host.docker.internal:5055` |
| Container on an explicitly shared Docker network | `http://seerr:5055` |

`localhost` inside MediaBot means the MediaBot container itself. A service name
such as `seerr` resolves only when the two containers share a Docker network.
The portable Compose file supplies `host.docker.internal` for the same-host
case; the provider still needs to listen on an address reachable from Docker.
See [Compose networking](https://docs.docker.com/compose/how-tos/networking/).

A public URL can be your members' authenticated HTTPS or VPN address. It does
not require exposing an API to the internet. Do not put passwords, API keys or
tokens in URL query strings. The wizard can also configure existing Jellyfin,
Sonarr and SoulSync instances. Leave integrations you do not use disabled.

## 3. Configure, check and start

Clone the repository, then choose a published stable tag from
[Releases](https://github.com/SableMayeux/mediabot/releases). Use that exact tag
in place of `vX.Y.Z`; release notes describe changes and migration requirements.

```sh
git clone https://github.com/SableMayeux/mediabot.git
cd mediabot
git checkout vX.Y.Z
python3 scripts/setup.py
python3 scripts/setup.py --check
python3 scripts/setup.py --start
python3 scripts/setup.py --status
```

Configuration creates a private `.env` and refuses to overwrite an existing
one. The check builds the image and tests connectivity/authentication from a
disposable bot container. It does not submit media requests. Startup initializes
the installation's named data volume and waits for the runtime healthcheck.
No Life, AI or torrent gateway is required for the basic installation.

Expected result: the `mediabot` service is running and healthy, and the bot is
online in your allowlisted server. Now send `$help` in its text channel. Finish
[the account-linking and first-request walkthrough](USAGE.md#your-first-request)
before inviting everyone to use it.

The owner can complete `$admin users` and `$admin link` in the bot's DM. The
member must already appear in Seerr after import or first login; a Jellyfin
account alone is not yet a request identity. DM administration verifies the
owner's current administrator role in the selected allowed server.

The named volume holds SQLite state, logs and default private captures. The
container uses UID/GID 1000 with a read-only root filesystem. The small
`data-init` service prepares the new volume; you do not need to chown a host
folder for the default installation.

For multiple independent instances on one host, use separate repository
directories and a unique project name:

```sh
python3 scripts/setup.py --project-name household-two
python3 scripts/setup.py --project-name household-two --check
python3 scripts/setup.py --project-name household-two --start
```

Keep using that project name for status, updates and raw Compose commands,
for example `docker compose -p household-two logs --tail 100 mediabot`.
Changing project names selects a different data volume; it does not migrate data.

## Updates and backups

Choose a published stable release, read its notes, and back up before changing
versions. Keep the checkout directory and Compose project name unchanged so the
same data volume is reused. Do not run `docker compose down -v` during upgrades;
it deletes the named data volume.

For the default project, this creates a consistent stopped-bot backup on Linux:

```sh
umask 077
backup_dir="$(pwd)/backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
git rev-parse HEAD > "$backup_dir/source-commit.txt"
cp .env compose.yaml "$backup_dir/"
docker compose stop mediabot
docker compose run --rm --no-deps -T --entrypoint tar mediabot \
  -czf - -C /app/data . > "$backup_dir/data.tar.gz"
tar -tzf "$backup_dir/data.tar.gz" > /dev/null
docker compose start mediabot
```

Check every command's result. If archiving fails, restart the bot and fix the
backup before upgrading. Copy the backup to a separate protected disk or backup
service. `.env` and private captures are secrets, so keep this directory out of
Git and public issue attachments. Custom bind mounts, gateways, Nextcloud and
the media files themselves need their own backups.

Then update:

```sh
git fetch --tags origin
git status --short
git checkout vX.Y.Z
python3 scripts/setup.py --check
python3 scripts/setup.py --start
```

Review local changes before checkout. The installer preserves `.env`; compare
new `.env.example` entries with your configuration when release notes require
them. `--check` performs the build before the running service is replaced.

To test a restore, check out the backed-up commit in a separate directory and
use a new Compose project name. Restore the saved `.env` privately, run its
`data-init` service, then extract `data.tar.gz` into that empty project's
`/app/data` volume using the same image and `tar -xzf - -C /app/data`.
Do not start the restored bot with the live bot token while the original is
running. Verify SQLite integrity and use a separate Discord test application,
or stop the original before a controlled cutover. Restore the matching database
when rolling back across a release with schema changes.

## If something does not work

Start with these diagnostics; redact private data before sharing output:

```sh
python3 scripts/setup.py --check
python3 scripts/setup.py --status
docker compose logs --tail 100 mediabot
```

| Symptom | Check |
| --- | --- |
| Bot stays offline or logs a privileged-intent error | Correct bot token, Message Content and Server Members intents, and any access review shown in the Developer Portal. |
| Bot leaves the server | `ALLOWED_GUILD_IDS` must contain that server's ID. |
| Bot is online but ignores `$help` | Correct prefix, Message Content intent, channel View/Send permissions, and the server allowlist. |
| Provider cannot be reached | Test the internal URL from the container with `--check`. Container `localhost` is not the host. Check provider listener, firewall and Docker networking. |
| Provider returns unauthorized/forbidden | Correct API key for that service and correct base URL. A successful webpage response does not prove API authentication. |
| Media result links open the wrong site | Correct the corresponding `*_PUBLIC_URL`, then recreate the service with `--start`. |
| Request says the user is not linked | Follow the owner-admin `$admin link` step in the user guide. Import the intended Jellyfin account into Seerr or have the member sign into Seerr first, then link its numeric Seerr ID. |
| A Jellyfin user is missing from `$admin users` | Check the Seerr **Users** page. Jellyfin users are not automatically enrolled by MediaBot. Complete Seerr import/first login, verify permissions, and refresh `$admin users`. |
| An admin command is denied in DM or asks for a server | You must currently administer an allowed server. With several eligible servers, use `$admin server <server ID>`. User listing/linking and sensitive diagnostics also require bot ownership. |
| Private command cannot delete its source | Grant Manage Messages in that channel, and permit DMs from the bot. The private workflow stops if source deletion fails. |
| `$ask`, `$life` or `$torrent` says unavailable | These need separately commissioned gateways. See the feature matrix in the README; enabling an environment variable does not install a service. |
| Torrent says completed or `stalledUP`, but the files seem missing | Check full torrent size, selected bytes and actual content/save paths. A torrent with all files skipped can show zero selected bytes without downloading its payload. `stalledUP` means waiting to upload; it alone does not prove all intended files exist. See the [torrent walkthrough](USAGE.md#optional-torrent-workflow). |

For a public issue, include the release tag, OS, Docker/Compose versions, enabled
integration names, relevant error ID and a redacted log excerpt. Do not upload
`.env`, full databases, raw captures, credentials or magnet links.

## Existing customized deployments

The installer rejects an existing container in the selected project if its
`/app/data` mount differs from that project's expected named volume, including
the original bind-mounted layout. Do not change the project name to bypass this
check; a fresh volume would not contain your existing state.

The portable `compose.yaml` does not recreate an existing homelab's special
networks, mounts or gateways. The prior integration layout is retained as the
standalone `deploy/compose.homelab.yaml` for operators already using that stack.
From the repository root, its explicit command is:

```sh
docker compose --project-directory . -f deploy/compose.homelab.yaml config --quiet
```

Do not combine it with the portable file as an override, or point the portable
installer at an existing custom production deployment. Follow that deployment's
reviewed migration procedure. Historical `scripts/deploy_v*.sh` files are
stack-specific transactional deployers, not the installation path in this guide.
