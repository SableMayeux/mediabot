"""Private opt-in task classification after a durable raw capture."""
import discord

from mediabot.services.life_auto import classify_capture, task_fields
from mediabot.services.life_workflow import LifeWorkflowError
from mediabot.services.local_ai import LocalAIError
from mediabot.ui.life_workflow import LifeLauncher, ProposalView, safe


async def promote_automatically(ctx, *, bot, model, service, capture, thought):
    options = dict(bot=bot, service=service, actor_id=ctx.author.id, guild_id=None)
    launcher = LifeLauncher(capture={"id": capture.capture_id, "title": capture.title,
        "created_at": capture.created_at}, **options) if service.enabled else None
    text = f"Captured. `{capture.capture_id[:8]}`"
    try:
        send = ctx.reply if ctx.guild is None else ctx.author.send
        message = await send(text + " Checking whether this clearly describes a task...",
            allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        if launcher:
            launcher.stop()
        await ctx.send(text + " I could not open a DM. No automatic action was submitted; use `$life`.")
        return
    async def update(content, **kwargs):
        try:
            await message.edit(content=content, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        except discord.HTTPException:
            pass  # Raw capture and any gateway receipt remain authoritative.
    if not model.enabled or not service.enabled:
        await update(text + " Automatic tasks are unavailable. Your raw thought is saved.", view=launcher)
        return
    try:
        title = await classify_capture(model, thought)
    except LocalAIError as exc:
        await update(text + " No task was submitted. " + str(exc), view=launcher)
        return
    if title is None:
        await update(text + " Kept as a note. No clear task was identified; nothing else was created.", view=launcher)
        return
    fields = task_fields(capture.capture_id, title)
    try:
        await service.request("create_task", actor_id=ctx.author.id, **fields)
    except LifeWorkflowError as exc:
        proposal = ProposalView(action="create_task", fields=fields, **options)
        proposal.children[0].label = "Retry same task"
        await update(text + " Nextcloud did not confirm the task. " + str(exc)
            + " This retry keeps the same receipt and fields.", embed=proposal.embed(), view=proposal)
        if launcher:
            launcher.stop()
        return
    embed = discord.Embed(title="Task created in Nextcloud", description=safe(title, 200))
    embed.add_field(name="Receipt", value=f"`{fields['request_id']}`", inline=False)
    embed.set_footer(text="Title quoted from your thought. No inferred dates or reminders. Original capture preserved.")
    await update(text, embed=embed, view=launcher)
