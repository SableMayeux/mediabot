"""Resolve current membership for personal media commands and controls."""
from discord.ext import commands
import discord


async def media_scope(bot, user, allowed_ids, selections, *, guild_id=None):
    selected = guild_id if guild_id is not None else selections.get(user.id)
    candidates = [selected] if selected is not None else sorted(allowed_ids)
    matches = []
    for identity in candidates:
        if identity not in allowed_ids:
            continue
        guild = bot.get_guild(identity)
        if guild is None:
            continue
        try:
            member = await guild.fetch_member(user.id)
        except discord.NotFound:
            continue
        except discord.HTTPException:
            raise commands.CheckFailure("I could not verify current server membership. Try again shortly.") from None
        if member.id == user.id and not member.bot:
            matches.append((guild, member))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise commands.CheckFailure("Current membership in a configured MediaBot server is required.")
    raise commands.CheckFailure("You belong to several MediaBot servers. Use `$server <server ID>` to choose one for this DM.")
