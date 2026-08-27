from __future__ import annotations

import math
import os
import re
import secrets
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mediabot.providers.seerr import SeerrProvider


class DiscoveryUsageError(ValueError):
    """Raised when random-request filters are invalid or ambiguous."""


@dataclass(frozen=True)
class RandomRequestOptions:
    media_type: str
    genre: str | None
    pool_size: int
    count: int


@dataclass(frozen=True)
class RandomRequestPick:
    items: tuple[dict[str, Any], ...]
    options: RandomRequestOptions
    eligible_count: int
    genre_name: str | None

    @property
    def item(self) -> dict[str, Any]:
        return self.items[0]


@dataclass(frozen=True)
class DiscoverOptions:
    media_type: str | None
    genre_expression: str
    pool_size: int
    count: int
    randomize: bool


@dataclass(frozen=True)
class DiscoverBatch:
    items: tuple[dict[str, Any], ...]
    options: DiscoverOptions
    eligible_count: int
    genre_filter: str | None

    @property
    def genre_name(self) -> str | None:
        return self.genre_filter


@dataclass(frozen=True)
class GenreBranch:
    tokens: tuple[str, ...]
    genre_ids: tuple[int, ...]
    label: str


class DiscoveryService:
    """Own ranked discovery and candidate selection policy."""

    TV_ROMANCE_GENRE_ID = -9840
    TV_ROMANCE_KEYWORD_IDS = (9840, 9673, 164663)

    MEDIA_TYPE_ALIASES = {
        "movie": "movie",
        "movies": "movie",
        "show": "tv",
        "shows": "tv",
        "tv": "tv",
        "series": "tv",
    }

    def __init__(
        self,
        seerr: SeerrProvider,
        *,
        default_pool_size: int | None = None,
        max_pool_size: int | None = None,
        max_count: int | None = None,
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> None:
        self.seerr = seerr
        self.default_pool_size = int(
            default_pool_size
            if default_pool_size is not None
            else os.environ.get("RANDOM_REQUEST_POOL_SIZE", "40")
        )
        self.max_pool_size = int(
            max_pool_size
            if max_pool_size is not None
            else os.environ.get("RANDOM_REQUEST_MAX_POOL", "200")
        )
        self.max_count = int(
            max_count
            if max_count is not None
            else os.environ.get("RANDOM_REQUEST_MAX_COUNT", "5")
        )
        self.randbelow = randbelow

        if self.default_pool_size < 1:
            raise ValueError("Default random-request pool must be positive.")

        if self.max_pool_size < self.default_pool_size:
            raise ValueError(
                "Maximum random-request pool cannot be smaller than default."
            )

        if self.max_count < 1 or self.max_count > 5:
            raise ValueError("Random-request maximum count must be 1 through 5.")

    def parse_random_request(self, raw: str) -> RandomRequestOptions:
        try:
            tokens = shlex.split(raw)
        except ValueError as exc:
            raise DiscoveryUsageError(str(exc)) from exc

        if not tokens:
            raise DiscoveryUsageError(
                "Choose `movie` or `show` before requesting a random pick."
            )

        media_type = self.MEDIA_TYPE_ALIASES.get(tokens.pop(0).casefold())

        if not media_type:
            raise DiscoveryUsageError(
                "The first argument must be `movie` or `show`."
            )

        pool_size = self.default_pool_size
        count = 1
        genre_tokens: list[str] = []
        index = 0

        while index < len(tokens):
            token = tokens[index]

            if token == "--top":
                index += 1

                if index >= len(tokens):
                    raise DiscoveryUsageError("`--top` requires a number.")

                raw_pool_size = tokens[index]

                try:
                    pool_size = int(raw_pool_size)
                except ValueError as exc:
                    raise DiscoveryUsageError(
                        "`--top` must be a whole number."
                    ) from exc

            elif token.startswith("--top="):
                try:
                    pool_size = int(token.partition("=")[2])
                except ValueError as exc:
                    raise DiscoveryUsageError(
                        "`--top` must be a whole number."
                    ) from exc

            elif token == "--count":
                index += 1

                if index >= len(tokens):
                    raise DiscoveryUsageError("`--count` requires a number.")

                try:
                    count = int(tokens[index])
                except ValueError as exc:
                    raise DiscoveryUsageError(
                        "`--count` must be a whole number."
                    ) from exc

            elif token.startswith("--count="):
                try:
                    count = int(token.partition("=")[2])
                except ValueError as exc:
                    raise DiscoveryUsageError(
                        "`--count` must be a whole number."
                    ) from exc

            elif token.startswith("--"):
                raise DiscoveryUsageError(f"Unknown option `{token}`.")

            else:
                genre_tokens.append(token)

            index += 1

        if not 1 <= pool_size <= self.max_pool_size:
            raise DiscoveryUsageError(
                "`--top` must be between 1 and "
                f"{self.max_pool_size}."
            )

        if not 1 <= count <= self.max_count:
            raise DiscoveryUsageError(
                f"`--count` must be between 1 and {self.max_count}."
            )

        if count > pool_size:
            raise DiscoveryUsageError(
                "`--count` cannot be larger than `--top`."
            )

        genre = " ".join(genre_tokens).strip() or None

        return RandomRequestOptions(
            media_type=media_type,
            genre=genre,
            pool_size=pool_size,
            count=count,
        )

    def parse_discover(self, raw: str) -> DiscoverOptions:
        try:
            tokens = shlex.split(raw)
        except ValueError as exc:
            raise DiscoveryUsageError(str(exc)) from exc

        media_type = None

        if tokens:
            media_type = self.MEDIA_TYPE_ALIASES.get(tokens[0].casefold())

            if media_type:
                tokens.pop(0)

        randomize = False
        pool_size = self.default_pool_size
        count = 1
        genre_parts: list[str] = []
        index = 0

        while index < len(tokens):
            token = tokens[index]

            if token.casefold() in ("--random", "-random"):
                randomize = True
            elif token in ("--top", "-top"):
                index += 1

                if index >= len(tokens):
                    raise DiscoveryUsageError("`--top` requires a number.")

                try:
                    pool_size = int(tokens[index])
                except ValueError as exc:
                    raise DiscoveryUsageError(
                        "`--top` must be a whole number."
                    ) from exc
            elif token.startswith(("--top=", "-top=")):
                try:
                    pool_size = int(token.partition("=")[2])
                except ValueError as exc:
                    raise DiscoveryUsageError(
                        "`--top` must be a whole number."
                    ) from exc
            elif token in ("--count", "-count"):
                index += 1

                if index >= len(tokens):
                    raise DiscoveryUsageError("`--count` requires a number.")

                try:
                    count = int(tokens[index])
                except ValueError as exc:
                    raise DiscoveryUsageError(
                        "`--count` must be a whole number."
                    ) from exc
            elif token.startswith(("--count=", "-count=")):
                try:
                    count = int(token.partition("=")[2])
                except ValueError as exc:
                    raise DiscoveryUsageError(
                        "`--count` must be a whole number."
                    ) from exc
            elif token.startswith("-"):
                raise DiscoveryUsageError(f"Unknown option `{token}`.")
            else:
                genre_parts.append(token)

            index += 1

        if not 1 <= pool_size <= self.max_pool_size:
            raise DiscoveryUsageError(
                f"`--top` must be between 1 and {self.max_pool_size}."
            )

        if not 1 <= count <= self.max_count:
            raise DiscoveryUsageError(
                f"`--count` must be between 1 and {self.max_count}."
            )

        if count > pool_size:
            raise DiscoveryUsageError(
                "`--count` cannot be larger than `--top`."
            )

        return DiscoverOptions(
            media_type=media_type,
            genre_expression=" ".join(genre_parts).strip(),
            pool_size=pool_size,
            count=count,
            randomize=randomize,
        )

    async def genre_catalog(
        self,
        media_type: str,
    ) -> list[dict[str, Any]]:
        normalized = self.MEDIA_TYPE_ALIASES.get(media_type.casefold())

        if not normalized:
            raise DiscoveryUsageError("Genre type must be `movie` or `show`.")

        genres = list(await self.seerr.genres(normalized))

        if normalized == "tv":
            genres.append({
                "id": self.TV_ROMANCE_GENRE_ID,
                "name": "Romance",
            })

        return genres

    @staticmethod
    def _normalize_genre(value: str) -> str:
        return "".join(character for character in value.casefold() if character.isalnum())

    async def resolve_genre(
        self,
        media_type: str,
        requested: str | None,
    ) -> tuple[int | None, str | None]:
        if not requested:
            return None, None

        genre_ids, genre_names = await self.resolve_genres(
            media_type,
            (requested,),
        )
        return genre_ids[0], genre_names[0]

    async def resolve_genres(
        self,
        media_type: str,
        requested: tuple[str, ...],
    ) -> tuple[tuple[int, ...], tuple[str, ...]]:
        if not requested:
            return (), ()

        genres = await self.genre_catalog(media_type)
        by_name = {
            self._normalize_genre(str(genre.get("name", ""))): genre
            for genre in genres
            if genre.get("id") is not None and genre.get("name")
        }
        aliases = {
            ("movie", "scifi"): "sciencefiction",
            ("tv", "scifi"): "scififantasy",
            ("tv", "sciencefiction"): "scififantasy",
            ("tv", "fantasy"): "scififantasy",
            ("tv", "action"): "actionadventure",
            ("tv", "adventure"): "actionadventure",
            ("movie", "love"): "romance",
            ("movie", "romantic"): "romance",
            ("tv", "love"): "romance",
            ("tv", "romantic"): "romance",
            ("movie", "musical"): "music",
            ("movie", "musicals"): "music",
        }
        resolved_ids: list[int] = []
        resolved_names: list[str] = []
        index = 0

        while index < len(requested):
            match = None

            for end in range(len(requested), index, -1):
                raw = " ".join(requested[index:end])
                wanted = self._normalize_genre(raw)
                wanted = aliases.get((media_type, wanted), wanted)
                genre = by_name.get(wanted)

                if genre:
                    match = (end, genre)
                    break

            if not match:
                names = ", ".join(
                    str(genre.get("name"))
                    for genre in genres
                )
                unresolved = " ".join(requested[index:])
                raise DiscoveryUsageError(
                    f"Unknown {media_type} genre near `{unresolved}`. "
                    f"Available: {names}"
                )

            index, genre = match
            genre_id = int(genre["id"])

            if genre_id not in resolved_ids:
                resolved_ids.append(genre_id)
                resolved_names.append(str(genre["name"]))

        return tuple(resolved_ids), tuple(resolved_names)

    @staticmethod
    def _type_aliases(media_type: str) -> dict[str, str]:
        aliases = {
            "scifi": "sciencefiction",
            "musical": "music",
            "musicals": "music",
        }

        if media_type == "movie":
            aliases.update({
                "love": "romance",
                "romantic": "romance",
            })
        else:
            aliases.update({
                "sciencefiction": "scififantasy",
                "fantasy": "scififantasy",
                "action": "actionadventure",
                "adventure": "actionadventure",
                "love": "romance",
                "romantic": "romance",
            })

        return aliases

    def _expression_elements(
        self,
        expression: str,
        catalogs: dict[str, list[dict[str, Any]]],
    ) -> list[tuple[str, str | None]]:
        if not expression.strip():
            return []

        scrubbed = expression.replace("||", "").replace("&&", "")

        if "|" in scrubbed or "&" in scrubbed:
            raise DiscoveryUsageError(
                "Use `||` for OR and `&&` for AND; single `|` or `&` "
                "is not accepted."
            )

        raw_tokens = re.findall(
            r"\|\||&&|[(),]|[^\s(),&|]+",
            expression,
        )
        global_names = {
            self._normalize_genre(str(genre.get("name", "")))
            for genres in catalogs.values()
            for genre in genres
            if genre.get("name")
        }
        global_aliases = {
            "love",
            "romantic",
            "scifi",
            "sciencefiction",
            "fantasy",
            "action",
            "adventure",
            "musical",
            "musicals",
        }
        syntax = {"(", ")", ",", "&&", "||"}
        elements: list[tuple[str, str | None]] = []
        index = 0

        while index < len(raw_tokens):
            token = raw_tokens[index]
            lowered = token.casefold()

            if token in syntax or lowered in ("and", "or"):
                if token == "(" or token == ")":
                    elements.append((token, None))
                elif token in (",", "&&") or lowered == "and":
                    elements.append(("AND", None))
                else:
                    elements.append(("OR", None))

                index += 1
                continue

            run_end = index

            while run_end < len(raw_tokens):
                candidate = raw_tokens[run_end]

                if (
                    candidate in syntax
                    or candidate.casefold() in ("and", "or")
                ):
                    break

                run_end += 1

            run = raw_tokens[index:run_end]
            run_index = 0

            while run_index < len(run):
                match = None

                for end in range(len(run), run_index, -1):
                    raw_genre = " ".join(run[run_index:end])
                    normalized = self._normalize_genre(raw_genre)

                    if normalized in global_names or normalized in global_aliases:
                        match = (end, normalized)
                        break

                if not match:
                    unresolved = " ".join(run[run_index:])
                    available = sorted({
                        str(genre.get("name"))
                        for genres in catalogs.values()
                        for genre in genres
                        if genre.get("name")
                    })
                    raise DiscoveryUsageError(
                        f"Unknown genre near `{unresolved}`. Available: "
                        + ", ".join(available)
                    )

                run_index, normalized = match
                elements.append(("ATOM", normalized))

            index = run_end

        with_implicit_and: list[tuple[str, str | None]] = []

        for element in elements:
            previous = with_implicit_and[-1][0] if with_implicit_and else None
            current = element[0]

            if previous in ("ATOM", ")") and current in ("ATOM", "("):
                with_implicit_and.append(("AND", None))

            with_implicit_and.append(element)

        return with_implicit_and

    def _expression_rpn(
        self,
        expression: str,
        catalogs: dict[str, list[dict[str, Any]]],
    ) -> list[tuple[str, str | None]]:
        elements = self._expression_elements(expression, catalogs)

        if not elements:
            return []

        output: list[tuple[str, str | None]] = []
        operators: list[tuple[str, str | None]] = []
        precedence = {"OR": 1, "AND": 2}
        expect_operand = True

        for element in elements:
            kind = element[0]

            if kind == "ATOM":
                if not expect_operand:
                    raise DiscoveryUsageError("Missing AND or OR between genres.")

                output.append(element)
                expect_operand = False
            elif kind == "(":
                if not expect_operand:
                    raise DiscoveryUsageError("Missing operator before `(`.")

                operators.append(element)
                expect_operand = True
            elif kind == ")":
                if expect_operand:
                    raise DiscoveryUsageError("Empty or incomplete parentheses.")

                while operators and operators[-1][0] != "(":
                    output.append(operators.pop())

                if not operators:
                    raise DiscoveryUsageError("Unmatched `)` in genre expression.")

                operators.pop()
                expect_operand = False
            else:
                if expect_operand:
                    raise DiscoveryUsageError(
                        f"`{kind}` needs a genre on both sides."
                    )

                while (
                    operators
                    and operators[-1][0] in precedence
                    and precedence[operators[-1][0]] >= precedence[kind]
                ):
                    output.append(operators.pop())

                operators.append(element)
                expect_operand = True

        if expect_operand:
            raise DiscoveryUsageError("Genre expression ends with an operator.")

        while operators:
            operator = operators.pop()

            if operator[0] == "(":
                raise DiscoveryUsageError("Unmatched `(` in genre expression.")

            output.append(operator)

        return output

    def compile_genre_expression(
        self,
        *,
        media_type: str,
        expression: str,
        catalogs: dict[str, list[dict[str, Any]]],
    ) -> tuple[tuple[tuple[int, ...], ...], str | None]:
        branches = self.compile_genre_branches(
            media_type=media_type,
            expression=expression,
            catalogs=catalogs,
        )

        if not branches:
            return (), None

        rendered = []

        for branch in branches:
            text = branch.label

            if len(branch.genre_ids) > 1 and len(branches) > 1:
                text = f"({text})"

            rendered.append(text)

        return (
            tuple(branch.genre_ids for branch in branches),
            " OR ".join(rendered),
        )

    def _expression_token_branches(
        self,
        expression: str,
        catalogs: dict[str, list[dict[str, Any]]],
    ) -> tuple[tuple[str, ...], ...]:
        rpn = self._expression_rpn(expression, catalogs)

        if not rpn:
            return ((),)

        stack: list[list[tuple[str, ...]]] = []

        for kind, value in rpn:
            if kind == "ATOM":
                stack.append([(str(value),)])
                continue

            if len(stack) < 2:
                raise DiscoveryUsageError("Invalid genre expression.")

            right = stack.pop()
            left = stack.pop()

            if kind == "AND":
                combined = [
                    tuple(dict.fromkeys(left_branch + right_branch))
                    for left_branch in left
                    for right_branch in right
                ]
            else:
                combined = left + right

            unique = list(dict.fromkeys(combined))

            if len(unique) > 16:
                raise DiscoveryUsageError(
                    "That genre expression expands to more than 16 OR "
                    "branches. Simplify the grouping."
                )

            stack.append(unique)

        if len(stack) != 1:
            raise DiscoveryUsageError("Invalid genre expression.")

        return tuple(stack[0])

    def compile_genre_branches(
        self,
        *,
        media_type: str,
        expression: str,
        catalogs: dict[str, list[dict[str, Any]]],
    ) -> tuple[GenreBranch, ...]:
        token_branches = self._expression_token_branches(expression, catalogs)

        if token_branches == ((),):
            return (GenreBranch(tokens=(), genre_ids=(), label=""),)

        catalog = catalogs[media_type]
        by_name = {
            self._normalize_genre(str(genre.get("name", ""))): genre
            for genre in catalog
            if genre.get("id") is not None and genre.get("name")
        }
        aliases = self._type_aliases(media_type)
        compiled = []

        for tokens in token_branches:
            genre_ids = []
            genre_names = []

            for token in tokens:
                wanted = aliases.get(token, token)
                genre = by_name.get(wanted)

                if not genre:
                    break

                genre_id = int(genre["id"])

                if genre_id not in genre_ids:
                    genre_ids.append(genre_id)
                    genre_names.append(str(genre["name"]))
            else:
                compiled.append(
                    GenreBranch(
                        tokens=tokens,
                        genre_ids=tuple(genre_ids),
                        label=" AND ".join(genre_names),
                    )
                )

        return tuple(compiled)

    @staticmethod
    def _requestable(item: dict[str, Any]) -> bool:
        if item.get("adult"):
            return False

        status = (item.get("mediaInfo") or {}).get("status")
        return status not in (2, 3, 4, 5, 6)

    async def candidate_pool(
        self,
        *,
        media_type: str | None,
        genre_expression: str,
        pool_size: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        media_types = (media_type,) if media_type else ("movie", "tv")
        pools: list[list[dict[str, Any]]] = []
        rendered_filter = None
        catalogs = {
            current_type: await self.genre_catalog(current_type)
            for current_type in ("movie", "tv")
        }
        expected_branches = set(
            self._expression_token_branches(
                genre_expression,
                catalogs,
            )
        )

        for current_type in media_types:
            branch_details = self.compile_genre_branches(
                media_type=current_type,
                expression=genre_expression,
                catalogs=catalogs,
            )
            compatible_branches = {
                branch.tokens
                for branch in branch_details
            }

            if (
                genre_expression
                and compatible_branches != expected_branches
            ):
                continue

            branches, current_filter = self.compile_genre_expression(
                media_type=current_type,
                expression=genre_expression,
                catalogs=catalogs,
            )

            if not branches:
                continue

            pool = await self._candidate_pool_for_branches(
                media_type=current_type,
                branches=branches,
                pool_size=pool_size,
            )
            pools.append(pool)

            if not rendered_filter:
                rendered_filter = current_filter

        if not pools:
            raise DiscoveryUsageError(
                "That genre expression has no valid branch for the selected "
                "movie/show scope."
            )

        if len(pools) == 1:
            return pools[0][:pool_size], rendered_filter

        candidates: list[dict[str, Any]] = []
        index = 0

        while len(candidates) < pool_size:
            added = False

            for pool in pools:
                if index < len(pool):
                    candidates.append(pool[index])
                    added = True

                    if len(candidates) >= pool_size:
                        break

            if not added:
                break

            index += 1

        return candidates, rendered_filter

    async def _candidate_pool_for_branches(
        self,
        *,
        media_type: str,
        branches: tuple[tuple[int, ...], ...],
        pool_size: int,
    ) -> list[dict[str, Any]]:
        pools = []

        for branch in branches:
            pools.append(
                await self._candidate_pool_for_type(
                    media_type=media_type,
                    genre_ids=branch,
                    pool_size=pool_size,
                )
            )

        if len(pools) == 1:
            return pools[0]

        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        index = 0

        while len(candidates) < pool_size:
            added = False

            for pool in pools:
                if index >= len(pool):
                    continue

                item = pool[index]
                key = (str(item.get("mediaType")), int(item.get("id") or 0))

                if key in seen:
                    continue

                seen.add(key)
                candidates.append(item)
                added = True

                if len(candidates) >= pool_size:
                    break

            if not added and all(index >= len(pool) - 1 for pool in pools):
                break

            index += 1

        return candidates

    async def _candidate_pool_for_type(
        self,
        *,
        media_type: str,
        genre_ids: tuple[int, ...],
        pool_size: int,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        tmdb_genre_ids = tuple(
            genre_id for genre_id in genre_ids if genre_id > 0
        )
        semantic_genre_ids = tuple(
            genre_id for genre_id in genre_ids if genre_id < 0
        )
        keyword_ids: tuple[int, ...] = ()

        if self.TV_ROMANCE_GENRE_ID in semantic_genre_ids:
            keyword_ids = self.TV_ROMANCE_KEYWORD_IDS
        seen: set[tuple[str, int]] = set()
        page = 1
        total_pages = 1
        maximum_pages = min(25, max(1, math.ceil(pool_size / 20) + 5))

        while (
            len(candidates) < pool_size
            and page <= total_pages
            and page <= maximum_pages
        ):
            discover_kwargs: dict[str, Any] = {
                "page": page,
                "genre_ids": tmdb_genre_ids,
            }

            if keyword_ids:
                discover_kwargs["keyword_ids"] = keyword_ids

            result = await self.seerr.discover(media_type, **discover_kwargs)
            total_pages = max(1, int(result.get("totalPages") or 1))

            for item in result.get("results") or []:
                item.setdefault("mediaType", media_type)

                if semantic_genre_ids:
                    item["genreIds"] = list(dict.fromkeys([
                        *(item.get("genreIds") or []),
                        *semantic_genre_ids,
                    ]))

                key = (str(item.get("mediaType")), int(item.get("id") or 0))

                if key in seen or not self._requestable(item):
                    continue

                seen.add(key)
                candidates.append(item)

                if len(candidates) >= pool_size:
                    break

            page += 1

        return candidates

    async def random_request(self, raw: str) -> RandomRequestPick | None:
        options = self.parse_random_request(raw)
        genre_id, genre_name = await self.resolve_genre(
            options.media_type,
            options.genre,
        )

        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        page = 1
        total_pages = 1
        maximum_pages = min(
            25,
            max(1, math.ceil(options.pool_size / 20) + 5),
        )

        while (
            len(candidates) < options.pool_size
            and page <= total_pages
            and page <= maximum_pages
        ):
            result = await self.seerr.discover(
                options.media_type,
                page=page,
                genre_id=genre_id,
            )
            total_pages = max(1, int(result.get("totalPages") or 1))

            for item in result.get("results") or []:
                item.setdefault("mediaType", options.media_type)
                key = (str(item.get("mediaType")), int(item.get("id") or 0))

                if key in seen or not self._requestable(item):
                    continue

                seen.add(key)
                candidates.append(item)

                if len(candidates) >= options.pool_size:
                    break

            page += 1

        if not candidates:
            return None

        selected: list[dict[str, Any]] = []
        swaps: dict[int, int] = {}
        remaining = len(candidates)

        for _ in range(min(options.count, len(candidates))):
            slot = self.randbelow(remaining)
            candidate_index = swaps.get(slot, slot)
            selected.append(candidates[candidate_index])
            remaining -= 1
            swaps[slot] = swaps.get(remaining, remaining)

        return RandomRequestPick(
            items=tuple(selected),
            options=options,
            eligible_count=len(candidates),
            genre_name=genre_name,
        )

    async def discover(self, raw: str) -> DiscoverBatch | None:
        options = self.parse_discover(raw)
        candidates, genre_filter = await self.candidate_pool(
            media_type=options.media_type,
            genre_expression=options.genre_expression,
            pool_size=options.pool_size,
        )

        if not candidates:
            return None

        if options.randomize:
            selected: list[dict[str, Any]] = []
            swaps: dict[int, int] = {}
            remaining = len(candidates)

            for _ in range(min(options.count, len(candidates))):
                slot = self.randbelow(remaining)
                candidate_index = swaps.get(slot, slot)
                selected.append(candidates[candidate_index])
                remaining -= 1
                swaps[slot] = swaps.get(remaining, remaining)
        else:
            selected = candidates[: options.count]

        return DiscoverBatch(
            items=tuple(selected),
            options=options,
            eligible_count=len(candidates),
            genre_filter=genre_filter,
        )
