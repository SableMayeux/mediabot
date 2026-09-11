"""Current trusted-server membership for shared chat, without Life access."""
import discord


async def allowed_chat_user(bot, user, allowed_guild_ids, *, guild_id=None):
    if guild_id is not None and guild_id not in allowed_guild_ids:
        return False
    if await bot.is_owner(user):
        return True
    candidates = (guild_id,) if guild_id is not None else sorted(allowed_guild_ids)
    for identity in candidates:
        guild = bot.get_guild(identity)
        if guild is None:
            continue
        try:
            # Fetch current membership: a removed member must lose DM controls,
            # even if an old Member object is still present in the local cache.
            member = await guild.fetch_member(user.id)
        except discord.HTTPException:
            continue
        if member.id == user.id and not member.bot:
            return True
    return False
