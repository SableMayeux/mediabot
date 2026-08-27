from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from mediabot.services.discovery import DiscoverOptions, DiscoveryService


@dataclass(frozen=True)
class RecommendationBatch:
    items: tuple[dict[str, Any], ...]
    options: DiscoverOptions
    eligible_count: int
    genre_name: str | None
    scores: dict[tuple[str, int], float]
    reasons: dict[tuple[str, int], str]
    signals: dict[str, Any]


class RecommendationService:
    """Score requestable Seerr discovery using local and remote taste signals."""

    def __init__(self, discovery: DiscoveryService) -> None:
        self.discovery = discovery

    @staticmethod
    def _normalize_genre(value: str) -> str:
        return "".join(
            character
            for character in str(value).casefold()
            if character.isalnum()
        )

    @staticmethod
    def _tmdb_id(item: dict[str, Any]) -> int | None:
        direct = item.get("id") if item.get("mediaType") else None

        if direct:
            try:
                return int(direct)
            except (TypeError, ValueError):
                pass

        providers = item.get("ProviderIds") or item.get("providerIds") or {}

        for key, value in providers.items():
            if str(key).casefold() == "tmdb":
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None

        ids = item.get("ids") or {}

        try:
            return int(ids.get("tmdb")) if ids.get("tmdb") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _candidate_key(item: dict[str, Any]) -> tuple[str, int]:
        return (str(item.get("mediaType")), int(item.get("id") or 0))

    def _select_ranked(self, entries, count: int):
        selected = []
        remaining = list(entries)
        uncovered = {
            branch
            for _, _, _, branches in entries
            for branch in branches
            if branch
        }

        while uncovered and remaining and len(selected) < count:
            best = max(
                remaining,
                key=lambda entry: (
                    len(set(entry[3]) & uncovered),
                    entry[0],
                ),
            )
            covered = set(best[3]) & uncovered

            if not covered:
                break

            selected.append(best)
            remaining.remove(best)
            uncovered.difference_update(covered)

        if len(selected) < count:
            selected.extend(remaining[:count - len(selected)])

        return selected

    def _select_random(self, entries, count: int):
        window = list(entries[: max(count, min(15, len(entries)))])
        selected = []

        while window and len(selected) < count:
            index = self.discovery.randbelow(len(window))
            selected.append(window.pop(index))

        return selected

    async def recommend(
        self,
        raw: str,
        *,
        ratings: list[dict[str, Any]],
        jellyfin_items: list[dict[str, Any]] | None = None,
        trakt_items: list[dict[str, Any]] | None = None,
        trakt_ratings: list[dict[str, Any]] | None = None,
        trakt_available: bool = False,
    ) -> RecommendationBatch | None:
        options = self.discovery.parse_discover(raw)
        candidates, genre_filter = await self.discovery.candidate_pool(
            media_type=options.media_type,
            genre_expression=options.genre_expression,
            pool_size=options.pool_size,
        )

        if not candidates:
            return None

        media_types = (options.media_type,) if options.media_type else ("movie", "tv")
        genre_names_by_type = {}
        genre_branches_by_type = {}
        catalogs = {
            current_type: await self.discovery.genre_catalog(current_type)
            for current_type in ("movie", "tv")
        }

        for media_type in media_types:
            genre_catalog = catalogs[media_type]
            genre_names_by_type[media_type] = {
                int(genre["id"]): str(genre["name"])
                for genre in genre_catalog
                if genre.get("id") is not None and genre.get("name")
            }
            genre_branches_by_type[media_type] = (
                self.discovery.compile_genre_branches(
                    media_type=media_type,
                    expression=options.genre_expression,
                    catalogs=catalogs,
                )
            )
        genre_weights: dict[str, float] = defaultdict(float)
        rated_keys: set[tuple[str, int]] = set()
        rating_count = 0
        trakt_rating_count = 0
        ratings_by_key: dict[tuple[str, int], dict[str, Any]] = {}

        for row in trakt_ratings or []:
            media_type = str(row.get("media_type"))

            if options.media_type and media_type != options.media_type:
                continue

            try:
                key = (media_type, int(row["tmdb_id"]))
                float(row["rating"])
            except (KeyError, TypeError, ValueError):
                continue

            ratings_by_key[key] = row
            trakt_rating_count += 1

        for row in ratings:
            if (
                options.media_type
                and str(row.get("media_type")) != options.media_type
            ):
                continue

            rating_count += 1
            key = (str(row.get("media_type")), int(row["tmdb_id"]))
            ratings_by_key[key] = row

        for key, row in ratings_by_key.items():
            rated_keys.add(key)
            weight = (float(row["rating"]) - 5.5) * 2.0

            for genre in str(row.get("genres") or "").split(","):
                normalized = self._normalize_genre(genre)
                if normalized:
                    genre_weights[normalized] += weight

        watched_keys: set[tuple[str, int]] = set()
        jellyfin_count = 0

        for item in jellyfin_items or []:
            item_media_type = {
                "Movie": "movie",
                "Series": "tv",
            }.get(item.get("Type"))

            if not item_media_type:
                continue

            if options.media_type and item_media_type != options.media_type:
                continue

            jellyfin_count += 1
            tmdb_id = self._tmdb_id(item)

            if tmdb_id:
                watched_keys.add((item_media_type, tmdb_id))

            user_data = item.get("UserData") or {}
            weight = 0.0

            if user_data.get("IsFavorite") or user_data.get("Likes") is True:
                weight += 4.0
            if user_data.get("Rating") is not None:
                weight += (float(user_data["Rating"]) - 5.5) * 2.0

            for genre in item.get("Genres") or []:
                normalized = self._normalize_genre(genre)
                if normalized:
                    genre_weights[normalized] += weight

        trakt_ranks: dict[tuple[str, int], int] = {}
        ranks_by_type: dict[str, int] = defaultdict(int)

        for item in trakt_items or []:
            item_type = str(item.get("mediaType"))
            tmdb_id = self._tmdb_id(item)

            if tmdb_id is None:
                continue

            key = (item_type, tmdb_id)

            if key not in trakt_ranks:
                ranks_by_type[item_type] += 1
                trakt_ranks[key] = ranks_by_type[item_type]
        scored: list[
            tuple[
                float,
                dict[str, Any],
                str,
                tuple[tuple[str, ...], ...],
            ]
        ] = []

        for rank, item in enumerate(candidates):
            candidate_type = str(item.get("mediaType"))
            tmdb_id = int(item.get("id") or 0)
            candidate_key = (candidate_type, tmdb_id)

            if candidate_key in rated_keys or candidate_key in watched_keys:
                continue

            candidate_genres = {
                int(genre_id)
                for genre_id in item.get("genreIds") or []
            }
            matched_filter_branches = tuple(
                branch
                for branch in genre_branches_by_type.get(candidate_type, ())
                if set(branch.genre_ids).issubset(candidate_genres)
            )

            if options.genre_expression and not matched_filter_branches:
                continue

            community = float(item.get("voteAverage") or 0.0)
            score = 20.0 - min(20.0, rank * 0.35) + community * 1.5
            matches: list[tuple[float, str]] = []

            for genre_id in item.get("genreIds") or []:
                name = genre_names_by_type.get(candidate_type, {}).get(
                    int(genre_id)
                )
                raw_weight = genre_weights.get(
                    self._normalize_genre(name or ""),
                    0.0,
                )
                weight = max(-10.0, min(10.0, raw_weight))

                if weight:
                    score += weight
                    matches.append((weight, name or str(genre_id)))

            trakt_rank = trakt_ranks.get(candidate_key)

            if trakt_rank is not None:
                score += 18.0 / (1.0 + (trakt_rank - 1) / 20.0)

            positive_matches = [
                name
                for weight, name in sorted(matches, reverse=True)
                if weight > 0
            ][:2]
            reason_parts = []

            filter_labels = list(dict.fromkeys(
                branch.label
                for branch in matched_filter_branches
                if branch.label
            ))

            if filter_labels:
                rendered_labels = [
                    f"({label})"
                    if len(filter_labels) > 1 and " AND " in label
                    else label
                    for label in filter_labels
                ]
                reason_parts.append(
                    "matches your genres: " + " OR ".join(rendered_labels)
                )

            if trakt_rank is not None:
                reason_parts.append(f"Trakt suggestion #{trakt_rank}")
            if positive_matches:
                reason_parts.append(
                    "matches your taste: " + ", ".join(positive_matches)
                )
            if community:
                reason_parts.append(f"community rating {community:.1f}/10")
            if not reason_parts:
                reason_parts.append("popular and not in your library")

            scored.append(
                (
                    score,
                    item,
                    " • ".join(reason_parts),
                    tuple(branch.tokens for branch in matched_filter_branches),
                )
            )

        if not scored:
            return None

        scored.sort(key=lambda entry: entry[0], reverse=True)

        selector = self._select_random if options.randomize else self._select_ranked
        by_type = {
            media_type: [
                entry
                for entry in scored
                if str(entry[1].get("mediaType")) == media_type
            ]
            for media_type in ("movie", "tv")
        }

        if (
            options.media_type is None
            and options.count >= 2
            and by_type["movie"]
            and by_type["tv"]
        ):
            first_type = max(
                ("movie", "tv"),
                key=lambda media_type: by_type[media_type][0][0],
            )
            quotas = {
                first_type: (options.count + 1) // 2,
                ("tv" if first_type == "movie" else "movie"): options.count // 2,
            }
            selected = []

            for media_type in ("movie", "tv"):
                selected.extend(selector(by_type[media_type], quotas[media_type]))

            if len(selected) < options.count:
                selected_keys = {
                    self._candidate_key(entry[1]) for entry in selected
                }
                leftovers = [
                    entry for entry in scored
                    if self._candidate_key(entry[1]) not in selected_keys
                ]
                selected.extend(selector(leftovers, options.count - len(selected)))

            selected.sort(key=lambda entry: entry[0], reverse=True)
        else:
            selected = selector(scored, options.count)

        score_map = {
            self._candidate_key(item): score
            for score, item, _, _ in selected
        }
        reason_map = {
            self._candidate_key(item): reason
            for _, item, reason, _ in selected
        }

        return RecommendationBatch(
            items=tuple(item for _, item, _, _ in selected),
            options=options,
            eligible_count=len(scored),
            genre_name=genre_filter,
            scores=score_map,
            reasons=reason_map,
            signals={
                "ratings": rating_count,
                "trakt_ratings": trakt_rating_count,
                "effective_ratings": len(ratings_by_key),
                "jellyfin": jellyfin_count,
                "trakt": len(trakt_ranks),
                "trakt_available": bool(trakt_available),
            },
        )
