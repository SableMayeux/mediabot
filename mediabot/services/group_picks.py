"""Deterministic group ranking over actual playable library records."""
import json
import re
import shlex
from dataclasses import dataclass


COMMON_GENRES = (
    'Action', 'Adventure', 'Animation', 'Comedy', 'Crime', 'Documentary',
    'Drama', 'Family', 'Fantasy', 'History', 'Horror', 'Music', 'Mystery',
    'Romance', 'Science Fiction', 'TV Movie', 'Thriller', 'War', 'Western',
)


def normalize_genre(value):
    normalized = re.sub(r'[\W_]+', ' ', str(value).casefold()).strip()
    return 'science fiction' if normalized in {'sci fi', 'scifi', 'sciencefiction'} else normalized


@dataclass(frozen=True)
class TonightOptions:
    genre_query: str = ''
    max_minutes: int = 150
    participant_ids: tuple[int, ...] = ()


def parse_runtime(value):
    compact = re.sub(r'\s+', '', str(value).casefold())
    minutes = re.fullmatch(r'(\d+)(?:m|min|minutes?)?', compact)
    hours = re.fullmatch(r'(\d+)h(?:(\d+)m)?', compact)
    clock = re.fullmatch(r'(\d+):(\d{2})', compact)
    if minutes:
        result = int(minutes[1])
    elif hours:
        result = int(hours[1]) * 60 + int(hours[2] or 0)
    elif clock and int(clock[2]) < 60:
        result = int(clock[1]) * 60 + int(clock[2])
    else:
        raise ValueError('Use minutes, 2h, 1h30m or 1:30 for the maximum runtime.')
    if not 30 <= result <= 300:
        raise ValueError('Maximum runtime must be 30 to 300 minutes.')
    return result


def parse_tonight_options(value):
    if len(value) > 600:
        raise ValueError('Keep the genre and participant list under 600 characters.')
    try:
        tokens = shlex.split(value)
    except ValueError as error:
        raise ValueError('Close any quotation marks around genre names.') from error
    genres, participants = [], set()
    runtime, index = None, 0
    while index < len(tokens):
        token = tokens[index]
        mention = re.fullmatch(r'<@!?(\d+)>', token)
        if mention:
            identity = int(mention[1])
            if identity <= 0:
                raise ValueError('Choose current Discord server members.')
            participants.add(identity)
        elif token.startswith('--'):
            flag, separator, inline = token.partition('=')
            if flag not in {'--under', '--time', '--runtime', '--genre', '--genres'}:
                raise ValueError(f'Unknown option {flag[:40]}. Use --time or --under for maximum runtime.')
            if not separator:
                index += 1
                if index >= len(tokens) or tokens[index].startswith('--'):
                    raise ValueError(f'{flag} needs a value.')
                inline = tokens[index]
            if flag in {'--under', '--time', '--runtime'}:
                if runtime is not None:
                    raise ValueError('Choose one maximum runtime, using --time or --under.')
                runtime = parse_runtime(inline)
            else:
                if not inline.strip():
                    raise ValueError('A genre option needs a genre name.')
                genres.append(inline)
        else:
            genres.append(token)
        index += 1
    return TonightOptions(' '.join(genres).strip(), runtime if runtime is not None else 150,
                          tuple(sorted(participants)))


def library_genres(items):
    names = {}
    for item in items:
        if item.get('Type') != 'Movie' or item.get('IsMissing') or item.get('IsVirtualItem'):
            continue
        for name in item.get('Genres') or []:
            text = ' '.join(str(name).split())
            if text and normalize_genre(text):
                names.setdefault(normalize_genre(text), text)
    return tuple(sorted(names.values(), key=str.casefold))


def resolve_tonight_genres(query, available=()):
    """Multiple genres mean any matching genre; unknown terms never broaden picks."""
    if not query.strip():
        return ()
    known = {normalize_genre(name): name for name in (*COMMON_GENRES, *available)}
    aliases = {key: value for key, value in known.items()}
    for alias in ('sci fi', 'scifi', 'sciencefiction'):
        aliases[alias] = known['science fiction']
    resolved = []
    for raw in re.split(r'[,|;]', query):
        words = re.sub(r'[\W_]+', ' ', raw.casefold()).strip().split()
        while words:
            matched = next((key for key in sorted(aliases, key=lambda key: -len(key.split()))
                            if words[:len(key.split())] == key.split()), None)
            if matched is None:
                raise ValueError(f'Unknown genre: {" ".join(words)[:60]}. Try Horror, Comedy or Science Fiction; use commas for several.')
            name = aliases[matched]
            if name not in resolved:
                resolved.append(name)
            words = words[len(matched.split()):]
    if not resolved:
        raise ValueError('Choose a genre name, or leave genres empty for any genre.')
    if len(resolved) > 5:
        raise ValueError('Choose up to five genres; movies can match any one of them.')
    return tuple(resolved)


async def load_tonight_catalog(provider, *, page_size=1000, max_items=20000):
    """Do not silently drop genres that happen to fall beyond the first page."""
    rows, seen, offset = [], set(), 0
    while offset < max_items:
        page = await provider.catalog(item_type='Movie', start_index=offset, limit=page_size,
                                      sort_by='SortName', sort_order='Ascending')
        batch = page.get('Items') or []
        if not batch:
            return rows
        added = [item for item in batch if item.get('Id') and str(item['Id']) not in seen]
        if not added:
            raise ValueError('Jellyfin returned the same catalog page again. Try again after its library scan finishes.')
        rows.extend(added)
        seen.update(str(item['Id']) for item in added)
        offset += len(batch)
        total = page.get('TotalRecordCount')
        if (isinstance(total, int) and offset >= total) or len(batch) < page_size:
            return rows
    raise ValueError('The movie catalog exceeds 20,000 entries. Use `$discover` with a genre to browse it.')


def rank_movies(items, profiles, *, max_minutes=150, count=3, genres=()):
    requested = {normalize_genre(name) for name in genres}
    affinities = []
    for ratings in profiles:
        genres = {}
        for rating in ratings:
            raw = rating.get("genres") or ""
            try:
                names = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else re.split(r"[,|]", raw) if isinstance(raw, str) else raw
            except ValueError:
                names = []
            for name in names:
                key = normalize_genre(name)
                if key:
                    genres.setdefault(key, []).append((float(rating["rating"]) - 5.5) / 4.5)
        affinities.append({key: sum(values) / len(values) for key, values in genres.items()})
    candidates = []
    for item in items:
        if item.get("Type") != "Movie" or item.get("IsMissing") or item.get("IsVirtualItem") or not item.get("Id"):
            continue
        minutes = float(item.get("RunTimeTicks") or 0) / 600000000
        if not 0 < minutes <= max_minutes:
            continue
        item_genres = {normalize_genre(name) for name in item.get("Genres") or []}
        if requested and not requested.intersection(item_genres):
            continue
        group = [sum(profile.get(name, 0) for name in item_genres) / max(1, len(item_genres)) for profile in affinities]
        # A poor fit for one participant matters more than another's strong like.
        fit = (sum(group) / len(group) + min(group)) / 2 if group else 0
        score = float(item.get("CommunityRating") or 0) + 2 * fit
        candidates.append((score, str(item.get("Id")), item))
    candidates.sort(key=lambda row: (-row[0], row[1]))
    return [item for _, _, item in candidates[:count]]
