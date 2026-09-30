"""Deterministic group ranking over actual playable library records."""
import json
import re


def rank_movies(items, profiles, *, max_minutes=150, count=3):
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
                key = str(name).strip().casefold()
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
        genres = {str(name).casefold() for name in item.get("Genres", [])}
        group = [sum(profile.get(name, 0) for name in genres) / max(1, len(genres)) for profile in affinities]
        # A poor fit for one participant matters more than another's strong like.
        fit = (sum(group) / len(group) + min(group)) / 2 if group else 0
        score = float(item.get("CommunityRating") or 0) + 2 * fit
        candidates.append((score, str(item.get("Id")), item))
    candidates.sort(key=lambda row: (-row[0], row[1]))
    return [item for _, _, item in candidates[:count]]
