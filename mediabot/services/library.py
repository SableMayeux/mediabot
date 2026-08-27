from __future__ import annotations

import asyncio
import secrets
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mediabot.providers.jellyfin import JellyfinProvider


class LibraryUsageError(ValueError):
    """Raised when library-random filters are invalid."""


@dataclass(frozen=True)
class RandomLibraryOptions:
    item_type: str | None
    genre: str | None
    count: int


@dataclass(frozen=True)
class AvailableDiscoverBatch:
    items: tuple[dict[str, Any], ...]
    options: Any
    eligible_count: int
    genre_filter: str | None


class LibraryService:
    """Provider-independent orchestration for the available media library."""

    MEDIA_KIND_ALIASES = {
        "all": None,
        "any": None,
        "movie": "Movie",
        "movies": "Movie",
        "show": "Series",
        "shows": "Series",
        "tv": "Series",
        "series": "Series",
    }

    def __init__(
        self,
        jellyfin: JellyfinProvider,
        *,
        randbelow: Callable[[int], int] = secrets.randbelow,
        max_count: int = 5,
        max_semantic_candidates: int = 80,
        semantic_timeout_seconds: float = 12.0,
    ) -> None:
        self.jellyfin = jellyfin
        self.randbelow = randbelow
        self.max_count = max_count
        self.max_semantic_candidates = max(1, int(max_semantic_candidates))
        self.semantic_timeout_seconds = max(0.1, float(semantic_timeout_seconds))

    @classmethod
    def parse_random_filters(
        cls,
        raw: str,
        *,
        max_count: int = 5,
    ) -> RandomLibraryOptions:
        try:
            tokens = shlex.split(raw)
        except ValueError as exc:
            raise LibraryUsageError(str(exc)) from exc

        count = 1
        filter_tokens: list[str] = []
        index = 0

        while index < len(tokens):
            token = tokens[index]

            if token == "--count":
                index += 1

                if index >= len(tokens):
                    raise LibraryUsageError("`--count` requires a number.")

                raw_count = tokens[index]

                try:
                    count = int(raw_count)
                except ValueError as exc:
                    raise LibraryUsageError(
                        "`--count` must be a whole number."
                    ) from exc

            elif token.startswith("--count="):
                try:
                    count = int(token.partition("=")[2])
                except ValueError as exc:
                    raise LibraryUsageError(
                        "`--count` must be a whole number."
                    ) from exc

            elif token.startswith("--"):
                raise LibraryUsageError(f"Unknown option `{token}`.")

            else:
                filter_tokens.append(token)

            index += 1

        if not 1 <= count <= max_count:
            raise LibraryUsageError(
                f"`--count` must be between 1 and {max_count}."
            )

        normalized = " ".join(filter_tokens)

        if not normalized:
            return RandomLibraryOptions(None, None, count)

        first, separator, remainder = normalized.partition(" ")
        kind = cls.MEDIA_KIND_ALIASES.get(first.casefold(), "unrecognized")

        if kind != "unrecognized":
            return RandomLibraryOptions(kind, remainder or None, count)

        return RandomLibraryOptions(None, normalized, count)

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return await self.jellyfin.search(query, limit=limit)

    async def latest(
        self,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return await self.jellyfin.latest(limit=limit)

    @staticmethod
    def _media_type(item: dict[str, Any]) -> str | None:
        return {
            "Movie": "movie",
            "Series": "tv",
        }.get(str(item.get("Type")))

    @staticmethod
    def _normalized_genres(item: dict[str, Any]) -> set[str]:
        normalized = {
            "".join(
                character
                for character in str(genre).casefold()
                if character.isalnum()
            )
            for genre in item.get("Genres") or []
        }
        normalized.discard("")

        if item.get("Type") == "Series":
            if normalized & {"fantasy", "sciencefiction", "scififantasy"}:
                normalized.add("scififantasy")
            if normalized & {"action", "adventure", "actionadventure"}:
                normalized.add("actionadventure")

        return normalized

    @staticmethod
    def _rank_key(item: dict[str, Any]) -> tuple[float, str, str]:
        try:
            rating = float(item.get("CommunityRating") or 0.0)
        except (TypeError, ValueError):
            rating = 0.0

        return (
            rating,
            str(item.get("DateCreated") or ""),
            str(item.get("Name") or ""),
        )

    async def discover(
        self,
        raw: str,
        *,
        expression_service: Any,
        semantic_genre_resolver: Callable[[dict[str, Any]], Any] | None = None,
    ) -> AvailableDiscoverBatch | None:
        """Rank currently available Jellyfin items using discover syntax."""
        options = expression_service.parse_discover(raw)
        item_type = {
            "movie": "Movie",
            "tv": "Series",
        }.get(options.media_type)
        result = await self.jellyfin.catalog(
            item_type=item_type,
            start_index=0,
            limit=5000,
            sort_by="CommunityRating,DateCreated",
            sort_order="Descending",
        )
        items = [
            item
            for item in result.get("Items") or []
            if self._media_type(item) is not None
        ]

        if not items:
            return None

        genre_names = {"movie": set(), "tv": {"Romance"}}

        for item in items:
            media_type = self._media_type(item)

            if media_type:
                genre_names[media_type].update(
                    str(genre) for genre in item.get("Genres") or [] if genre
                )

        # Keep the expression aliases valid against Jellyfin's sometimes
        # split genre names (Fantasy vs. TMDB's Sci-Fi & Fantasy).
        genre_names["tv"].update({"Sci-Fi & Fantasy", "Action & Adventure"})
        catalogs = {}

        for media_type, names in genre_names.items():
            catalogs[media_type] = [
                {"id": index, "name": name}
                for index, name in enumerate(sorted(names), start=1)
            ]

        expected_branches = set(
            expression_service._expression_token_branches(
                options.genre_expression,
                catalogs,
            )
        )
        branches_by_type = {}
        ids_by_type = {}
        rendered_filter = None

        for media_type in ("movie", "tv"):
            branches = expression_service.compile_genre_branches(
                media_type=media_type,
                expression=options.genre_expression,
                catalogs=catalogs,
            )
            compatible = {branch.tokens for branch in branches}

            if options.genre_expression and compatible != expected_branches:
                branches = ()

            branches_by_type[media_type] = branches
            ids_by_type[media_type] = {
                expression_service._normalize_genre(str(genre["name"])): int(genre["id"])
                for genre in catalogs[media_type]
            }

            if branches and not rendered_filter:
                labels = [branch.label for branch in branches if branch.label]

                if labels:
                    rendered_filter = " OR ".join(
                        f"({label})"
                        if len(labels) > 1 and " AND " in label
                        else label
                        for label in labels
                    )

        prepared = []
        semantic_candidates = []

        for item in items:
            media_type = self._media_type(item)
            branches = branches_by_type.get(media_type, ())

            if not branches:
                continue

            normalized = self._normalized_genres(item)
            item_ids = {
                genre_id
                for genre in normalized
                if (genre_id := ids_by_type[media_type].get(genre)) is not None
            }
            prepared.append((item, media_type, item_ids))

            if media_type != "tv" or semantic_genre_resolver is None:
                continue

            romance_id = ids_by_type["tv"].get("romance")

            if romance_id is None or romance_id in item_ids:
                continue

            if any(
                romance_id in branch.genre_ids
                and set(branch.genre_ids) - {romance_id} <= item_ids
                for branch in branches
            ):
                semantic_candidates.append(item)

        semantic_matches: dict[str, set[str]] = {}

        if semantic_candidates and semantic_genre_resolver is not None:
            semantic_candidates.sort(key=self._rank_key, reverse=True)
            candidate_limit = min(
                self.max_semantic_candidates,
                max(options.pool_size, options.count),
            )
            bounded_candidates = semantic_candidates[:candidate_limit]
            tasks = {
                asyncio.create_task(semantic_genre_resolver(item)): item
                for item in bounded_candidates
            }
            done, pending = await asyncio.wait(
                tasks,
                timeout=self.semantic_timeout_seconds,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            for task in done:
                item = tasks[task]
                try:
                    extra_genres = task.result()
                except Exception:
                    continue

                semantic_matches[str(item.get("Id"))] = {
                    expression_service._normalize_genre(str(genre))
                    for genre in extra_genres or ()
                }

        eligible = []

        for item, media_type, item_ids in prepared:
            for genre in semantic_matches.get(str(item.get("Id")), set()):
                genre_id = ids_by_type[media_type].get(genre)

                if genre_id is not None:
                    item_ids.add(genre_id)

            if any(
                set(branch.genre_ids).issubset(item_ids)
                for branch in branches_by_type[media_type]
            ):
                eligible.append(item)

        if not eligible:
            return None

        eligible.sort(key=self._rank_key, reverse=True)
        pool = eligible[:options.pool_size]

        def select(source: list[dict[str, Any]], count: int):
            if not options.randomize:
                return source[:count]

            return [source[index] for index in self._sample_indices(len(source), count)]

        by_type = {
            media_type: [
                item for item in pool if self._media_type(item) == media_type
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
                key=lambda value: self._rank_key(by_type[value][0]),
            )
            other_type = "tv" if first_type == "movie" else "movie"
            quotas = {
                first_type: (options.count + 1) // 2,
                other_type: options.count // 2,
            }
            selected = []

            for media_type in ("movie", "tv"):
                selected.extend(select(by_type[media_type], quotas[media_type]))

            if len(selected) < options.count:
                selected_ids = {str(item.get("Id")) for item in selected}
                leftovers = [
                    item
                    for item in pool
                    if str(item.get("Id")) not in selected_ids
                ]
                selected.extend(select(leftovers, options.count - len(selected)))

            selected.sort(key=self._rank_key, reverse=True)
        else:
            selected = select(pool, options.count)

        return AvailableDiscoverBatch(
            items=tuple(selected),
            options=options,
            eligible_count=len(eligible),
            genre_filter=rendered_filter,
        )

    async def random_item(
        self,
        *,
        item_type: str | None = None,
        genre: str | None = None,
    ) -> tuple[dict[str, Any] | None, int]:
        items, total = await self.random_items(
            item_type=item_type,
            genre=genre,
            count=1,
        )
        return (items[0] if items else None), total

    def _sample_indices(
        self,
        total: int,
        count: int,
    ) -> list[int]:
        selected: list[int] = []
        swaps: dict[int, int] = {}
        remaining = total

        for _ in range(min(count, total)):
            slot = self.randbelow(remaining)
            selected.append(swaps.get(slot, slot))
            remaining -= 1
            swaps[slot] = swaps.get(remaining, remaining)

        return selected

    async def random_items(
        self,
        *,
        item_type: str | None = None,
        genre: str | None = None,
        count: int = 1,
    ) -> tuple[list[dict[str, Any]], int]:
        if not 1 <= count <= self.max_count:
            raise LibraryUsageError(
                f"Count must be between 1 and {self.max_count}."
            )

        first_page = await self.jellyfin.catalog(
            item_type=item_type,
            genre=genre,
            start_index=0,
            limit=1,
        )
        total = int(first_page.get("TotalRecordCount") or 0)

        if total <= 0:
            return [], 0

        indices = self._sample_indices(total, count)
        selected_items: list[dict[str, Any]] = []

        for selected_index in indices:
            if selected_index == 0:
                page = first_page
            else:
                page = await self.jellyfin.catalog(
                    item_type=item_type,
                    genre=genre,
                    start_index=selected_index,
                    limit=1,
                )

            items = page.get("Items") or []

            if items:
                selected_items.append(items[0])

        return selected_items, total
