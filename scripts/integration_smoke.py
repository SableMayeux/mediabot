import asyncio
import os

from mediabot.providers.jellyfin import JellyfinProvider
from mediabot.providers.seerr import SeerrProvider
from mediabot.services.discovery import DiscoveryService
from mediabot.services.recommendations import RecommendationService


async def main():
    seerr = SeerrProvider()
    jellyfin = JellyfinProvider()
    await seerr.start()
    await jellyfin.start()

    try:
        print("SEERR_OK", bool(await seerr.health()))
        taste_username = os.environ.get("JELLYFIN_TASTE_USER", "").strip()
        if not taste_username:
            raise RuntimeError("JELLYFIN_TASTE_USER must be set for this smoke test.")
        user = await jellyfin.resolve_user(taste_username)
        print("JELLYFIN_USER", user.get("Name") if user else None)

        if not user:
            raise RuntimeError("Configured Jellyfin taste user was not found.")

        taste = await jellyfin.user_taste_items(user["Id"], limit=50)
        print("TASTE_ITEMS", len(taste))
        discovery = DiscoveryService(seerr)
        batch = await discovery.discover("movie --top 10 --count 2")
        print(
            "DISCOVER",
            [(item.get("title"), item.get("id")) for item in batch.items],
        )
        niche = await discovery.discover(
            "horror comedy love --top 20 --count 5"
        )
        required_genres = {27, 35, 10749}
        print(
            "NICHE_AND_DISCOVERY",
            [item.get("title") for item in niche.items],
        )
        assert all(
            required_genres.issubset(set(item.get("genreIds") or []))
            for item in niche.items
        )
        boolean_filter = await discovery.discover(
            "(love comedy) || horror --top 20 --count 5"
        )
        print(
            "BOOLEAN_DISCOVERY",
            boolean_filter.genre_filter,
            [item.get("title") for item in boolean_filter.items],
        )
        assert all(
            (
                {10749, 35}.issubset(set(item.get("genreIds") or []))
                or 27 in set(item.get("genreIds") or [])
            )
            for item in boolean_filter.items
        )
        recommendation = await RecommendationService(discovery).recommend(
            "movie --top 10 --count 2",
            ratings=[],
            jellyfin_items=taste,
        )
        print(
            "RECOMMEND",
            [
                (item.get("title"), item.get("id"))
                for item in recommendation.items
            ],
            recommendation.signals,
        )
        niche_recommendation = await RecommendationService(discovery).recommend(
            "horror comedy love --top 20 --count 3",
            ratings=[],
            jellyfin_items=taste,
        )
        print(
            "NICHE_AND_RECOMMEND",
            [item.get("title") for item in niche_recommendation.items],
        )
        assert all(
            required_genres.issubset(set(item.get("genreIds") or []))
            for item in niche_recommendation.items
        )
        boolean_recommendation = await RecommendationService(discovery).recommend(
            "(love comedy) || horror --top 20 --count 5",
            ratings=[],
            jellyfin_items=taste,
        )
        print(
            "BOOLEAN_RECOMMEND",
            boolean_recommendation.genre_name,
            [item.get("title") for item in boolean_recommendation.items],
        )
        assert all(
            (
                {10749, 35}.issubset(set(item.get("genreIds") or []))
                or 27 in set(item.get("genreIds") or [])
            )
            for item in boolean_recommendation.items
        )

        try:
            trakt = await jellyfin.trakt_recommendations(user["Id"], "movie")
            print("TRAKT_RECOMMENDATIONS", len(trakt))
        except Exception as exc:
            print("TRAKT_UNAUTHORIZED", type(exc).__name__, str(exc)[:160])
    finally:
        await jellyfin.close()
        await seerr.close()


if __name__ == "__main__":
    asyncio.run(main())
