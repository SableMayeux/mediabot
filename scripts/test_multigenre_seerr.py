import asyncio

from mediabot.providers.seerr import SeerrProvider


async def main():
    seerr = SeerrProvider()
    await seerr.start()

    try:
        result = await seerr.request(
            "GET",
            "/discover/movies",
            params={
                "page": 1,
                "sortBy": "popularity.desc",
                "genre": "27,35,10749",
            },
        )
        wanted = {27, 35, 10749}
        items = result.get("results") or []
        print("RESULTS", len(items))
        print(
            "ALL_MATCH",
            all(wanted.issubset(set(item.get("genreIds") or [])) for item in items),
        )
        print(
            "SAMPLE",
            [
                (item.get("title"), item.get("id"), item.get("genreIds"))
                for item in items[:5]
            ],
        )
    finally:
        await seerr.close()


if __name__ == "__main__":
    asyncio.run(main())
