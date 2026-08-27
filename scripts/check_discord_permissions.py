import asyncio
import os

import aiohttp


async def main():
    token = os.environ["DISCORD_TOKEN"]

    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bot {token}"}
    ) as session:
        async with session.get(
            "https://discord.com/api/v10/users/@me/guilds"
        ) as response:
            response.raise_for_status()
            guilds = await response.json()

    for guild in guilds:
        permissions = int(guild.get("permissions") or 0)
        print(
            "GUILD_PERMISSIONS",
            guild.get("name"),
            "administrator=",
            bool(permissions & (1 << 3)),
            "manage_messages=",
            bool(permissions & (1 << 13)),
        )


if __name__ == "__main__":
    asyncio.run(main())
