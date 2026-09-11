#!/usr/bin/env python3
"""Configure and operate a standalone MediaBot install using Docker Compose.

Only Python's standard library is required on the host. Credentials never enter
command arguments, generated shell code, diagnostic output, or the image build.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import warnings
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    'SEERR_PUBLIC_URL': '', 'JELLYFIN_URL': '', 'JELLYFIN_API_KEY': '',
    'JELLYFIN_PUBLIC_URL': '', 'JELLYFIN_TASTE_USER': '',
    'SONARR_URL': '', 'SONARR_API_KEY': '', 'SOULSYNC_URL': '',
    'SOULSYNC_API_KEY': '', 'SOULSYNC_PUBLIC_URL': '', 'TZ': 'UTC',
    'LIFE_CAPTURE_PATH': '/app/data/life-inbox',
    'LIFE_GATEWAY_URL': '', 'LOCAL_AI_URL': '', 'TORRENT_INTAKE_URL': '',
    'NEXTCLOUD_PUBLIC_ORIGIN': '',
}


class SetupError(RuntimeError):
    pass


def env_quote(value: str) -> str:
    if not isinstance(value, str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise SetupError('Configuration values must be single-line text without control characters.')
    # Compose double-quoted dotenv syntax: escaped backslash/quote, literal $$.
    # Exercised against real Compose by scripts/test_fresh_install.py.
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '$$') + '"'


def write_environment(path: Path, values: dict[str, str]) -> None:
    """Create a new private file, never replace an existing file or symlink."""
    if any(not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) for key in values):
        raise SetupError('Invalid configuration variable name.')
    content = '# Private MediaBot configuration. Do not commit or share this file.\n'
    content += ''.join(f'{key}={env_quote(value)}\n' for key, value in values.items())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise SetupError('.env already exists. Edit it locally, then run --check; setup never overwrites it.') from None
    try:
        if os.name == 'nt':
            identity = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'],
                text=True, capture_output=True, check=True, timeout=10)
            sid = next(csv.reader(identity.stdout.splitlines()))[1]
            if not re.fullmatch(r'S-1-5-[0-9-]+', sid):
                raise SetupError('Could not identify the Windows account for private file permissions.')
            subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r',
                f'*{sid}:(F)', '*S-1-5-18:(F)'], capture_output=True, check=True, timeout=10)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if fd != -1:
            os.close(fd)
        # Only this newly-created target can be removed here, never an old .env.
        path.unlink(missing_ok=True)
        raise SetupError('Could not create private .env with restricted permissions.') from None


def validate_url(value: str, *, required: bool = True) -> str:
    value = value.strip().rstrip('/')
    if not value and not required:
        return ''
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(ch.isspace() for ch in value)):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except ValueError:
        raise SetupError('Use a complete http:// or https:// URL without credentials, query or fragment.') from None
    if parsed.hostname in {'localhost', '127.0.0.1', '::1'}:
        raise SetupError('Inside the bot, localhost means the bot container. Use host.docker.internal or the server LAN address.')
    return value


def guild_ids(value: str) -> str:
    parts = [part.strip() for part in value.split(',')]
    if not parts or any(not re.fullmatch(r'[1-9][0-9]{9,19}', part) for part in parts):
        raise SetupError('Enter Discord server IDs, separated by commas (Developer Mode > Copy Server ID).')
    return ','.join(dict.fromkeys(parts))


def project_name(value: str) -> str:
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', value):
        raise SetupError('Project name must use lowercase letters, digits, hyphens or underscores, starting with a letter or digit.')
    return value


def prompt(label: str, *, default: str = '', secret: bool = False, validate=None, optional=False) -> str:
    while True:
        if secret:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', getpass.GetPassWarning)
                    value = getpass.getpass(label + ': ').strip()
            except getpass.GetPassWarning:
                raise SetupError('This terminal cannot hide credentials. Run setup in a terminal with working password input.') from None
        else:
            value = input(label + (f' [{default}]' if default else '') + ': ').strip()
        value = value or default
        if not value and not optional:
            print('A value is required.')
            continue
        try:
            env_quote(value)
            return validate(value) if validate else value
        except SetupError as exc:
            print(str(exc))


def configure(root: Path, name: str | None) -> None:
    if os.path.lexists(root / '.env'):
        raise SetupError('.env already exists. Edit it locally, then run --check; setup never overwrites it.')
    if not sys.stdin.isatty():
        raise SetupError('Run configuration in an interactive terminal so tokens can be entered without echo.')
    print('Configure your own Discord application and existing media services. No server login passwords are needed.')
    print('Use service URLs reachable from a Docker container; public URLs must work for Discord members.')
    values = dict(DEFAULTS)
    default_name = re.sub(r'[^a-z0-9_-]', '-', root.name.lower()).strip('-_') or 'mediabot'
    values['COMPOSE_PROJECT_NAME'] = name or prompt('Instance name', default=default_name[:63], validate=project_name)
    values['DISCORD_TOKEN'] = prompt('Discord bot token (hidden)', secret=True)
    values['ALLOWED_GUILD_IDS'] = prompt('Allowed Discord server IDs', validate=guild_ids)
    values['SEERR_URL'] = prompt('Seerr API base URL', validate=validate_url)
    values['SEERR_API_KEY'] = prompt('Seerr API key (hidden)', secret=True)
    values['SEERR_PUBLIC_URL'] = prompt('Seerr browser URL for members', default=values['SEERR_URL'], validate=validate_url)
    values['TZ'] = prompt('Timezone (IANA name, checked in the container)', default='UTC')
    for prefix, label in [('JELLYFIN', 'Jellyfin library'), ('SONARR', 'Sonarr episode repair'), ('SOULSYNC', 'SoulSync music')]:
        url = prompt(f'{label} API base URL (blank disables)', optional=True,
                     validate=lambda value: validate_url(value, required=False))
        values[prefix + '_URL'] = url
        if url:
            values[prefix + '_API_KEY'] = prompt(f'{label} API key (hidden)', secret=True)
            if prefix != 'SONARR':
                values[prefix + '_PUBLIC_URL'] = prompt(f'{label} browser URL (blank uses API URL)', optional=True,
                    validate=lambda value: validate_url(value, required=False))
    write_environment(root / '.env', values)
    print('Created private .env. Optional AI, Life workflow and torrent gateways remain disabled.')
    print('Next: python3 scripts/setup.py --check, then python3 scripts/setup.py --start.')


def run(args: list[str], label: str, *, env=None, timeout=600, display=False) -> str:
    try:
        result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError(f'{label} could not finish. Check Docker availability and your connection.') from None
    if result.returncode:
        # Docker's dotenv errors can include the literal bad line, including a
        # secret. Never echo arbitrary subprocess error output.
        hint = (' Docker socket permission denied; use an account authorized to run Docker.'
                if 'permission denied' in result.stderr.lower() else '')
        raise SetupError(f'{label} failed (exit {result.returncode}).{hint} See docs/INSTALL.md for diagnosis.')
    if display:
        print(result.stdout.strip())
    return result.stdout


def prerequisites() -> None:
    if shutil.which('docker') is None:
        raise SetupError('Docker is not installed. Install Docker Engine + Compose v2, or Docker Desktop with Linux containers, then rerun.')
    version = run(['docker', 'compose', 'version', '--short'], 'Docker Compose version check', timeout=20)
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (2, 20, 0):
        raise SetupError('Docker Compose 2.20 or newer is required.')
    if run(['docker', 'info', '--format', '{{.OSType}}'], 'Docker daemon check', timeout=20).strip() != 'linux':
        raise SetupError('MediaBot requires Linux containers. Switch Docker Desktop to Linux containers.')


def compose_context(name: str | None) -> tuple[list[str], dict[str, str]]:
    path = ROOT / '.env'
    if path.is_symlink() or not path.is_file():
        raise SetupError('A regular .env is required. Run python3 scripts/setup.py first.')
    if os.name != 'nt' and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SetupError('.env is readable by other accounts. Run chmod 600 .env before continuing.')
    # Prevent unrelated exported secrets/Compose selection from changing which
    # instance this installer configures. Docker connection/context vars survive.
    keys = set(re.findall(r'^([A-Z][A-Z0-9_]*)\s*=', path.read_text(encoding='utf-8'), re.MULTILINE))
    keys |= set(DEFAULTS) | {'DISCORD_TOKEN', 'SEERR_API_KEY', 'ALLOWED_GUILD_IDS', 'SEERR_URL', 'EVENT_TIMEZONE', 'LIFE_TIMEZONE'}
    env = {key: value for key, value in os.environ.items() if key not in keys and not key.startswith('COMPOSE_')}
    command = ['docker', 'compose', '--project-directory', str(ROOT), '--env-file', str(path), '-f', str(ROOT / 'compose.yaml')]
    if name:
        command += ['--project-name', project_name(name)]
    return command, env


def reject_legacy_data_mount(command, env):
    """A fresh installer must not replace an old bind-mounted deployment."""
    ids = run(command + ['ps', '-a', '-q', 'mediabot'], 'Existing installation check', env=env, timeout=20).split()
    model = json.loads(run(command + ['config', '--format', 'json'], 'Compose validation', env=env, timeout=20))
    expected = model['volumes']['mediabot_data']['name']
    for identity in ids:
        if not re.fullmatch(r'[0-9a-f]{12,64}', identity):
            raise SetupError('Docker returned an invalid container identity.')
        mounts = json.loads(run(['docker', 'inspect', '--format', '{{json .Mounts}}', identity], 'Data mount check', timeout=20))
        data = [mount for mount in mounts if mount.get('Destination') == '/app/data']
        if len(data) != 1 or data[0].get('Type') != 'volume' or data[0].get('Name') != expected:
            raise SetupError('This project already uses a different data mount. Preserve its deployment; follow the existing-installation migration notes before using standalone setup.')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--check', action='store_true', help='Build and test configured services without logging the bot into Discord')
    actions.add_argument('--start', action='store_true', help='Build, initialize private persistent storage and start the bot')
    actions.add_argument('--status', action='store_true', help='Show this installation\'s container state')
    def argument_project_name(value):
        try:
            return project_name(value)
        except SetupError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
    parser.add_argument('--project-name', type=argument_project_name, help='Explicit independent Compose instance name')
    args = parser.parse_args(argv)
    try:
        prerequisites()
        if not any((args.check, args.start, args.status)):
            configure(ROOT, args.project_name)
            return 0
        command, env = compose_context(args.project_name)
        run(command + ['config', '--quiet'], 'Compose configuration validation', env=env, timeout=20)
        if args.status:
            raw = run(command + ['ps', '-a', '--format', 'json'], 'Status query', env=env, timeout=20).strip()
            rows = json.loads(raw) if raw.startswith('[') else [json.loads(line) for line in raw.splitlines() if line.strip()]
            if not rows:
                print('No containers exist for this instance. Use --start after --check succeeds.')
            for row in rows:
                print(f"{row.get('Service')}: {row.get('State')} ({row.get('Health') or 'no health check'})")
            return 0
        reject_legacy_data_mount(command, env)
        print('Building the version checked out in this directory...')
        run(command + ['build'], 'Image build', env=env)
        if args.check:
            result = subprocess.run(command + ['run', '--rm', '--no-deps', '-T', '--entrypoint', 'python', 'mediabot', '-m', 'mediabot.core.setup_check'],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
            # Only structured, bounded doctor rows are allowed into terminal output.
            try:
                rows = json.loads(result.stdout)
                if not isinstance(rows, list) or not rows or len(rows) > 100:
                    raise ValueError
                for row in rows:
                    if (not isinstance(row, dict) or set(row) != {'check', 'ok', 'detail'}
                            or type(row['ok']) is not bool
                            or not isinstance(row['check'], str) or not isinstance(row['detail'], str)
                            or len(row['check']) > 100 or len(row['detail']) > 1500
                            or any(ord(ch) < 32 or ord(ch) == 127 for ch in row['check'] + row['detail'])):
                        raise ValueError
                for row in rows:
                    print(f"{'OK' if row['ok'] else 'FAIL'} {row['check']}: {row['detail']}")
            except (ValueError, TypeError, KeyError):
                raise SetupError('The diagnostic container did not return a usable report. Check Docker and docs/INSTALL.md.') from None
            return 0 if result.returncode == 0 and all(row['ok'] for row in rows) else 1
        print('Starting MediaBot and waiting for its Discord/runtime health check...')
        run(command + ['up', '-d', '--wait', '--wait-timeout', '150'], 'Bot startup and health check', env=env, timeout=180)
        print('MediaBot is healthy. In your allowed Discord server, send $help. See docs/USAGE.md to link request accounts.')
        return 0
    except (SetupError, KeyboardInterrupt, EOFError, subprocess.TimeoutExpired) as exc:
        print(str(exc) if isinstance(exc, SetupError) else 'Setup interrupted or timed out; existing data is preserved.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
