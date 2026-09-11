"""Requester-bound local conversation in its original channel or private DM."""
from __future__ import annotations

import asyncio
from functools import partial
import uuid

import discord

from mediabot.services.local_ai import LocalAIError
from mediabot.ui.life_workflow import OwnerView

_CURRENT = object()


def append_prompt(history, prompt):
    text = str(prompt).strip()
    if not text or len(text.encode("utf-8")) > 3000:
        raise LocalAIError("Use a nonempty question under 3000 UTF-8 bytes.")
    messages = [dict(item) for item in history] + [{"role": "user", "content": text}]
    while len(messages) > 12 or sum(len(item["content"].encode("utf-8")) for item in messages) > 3000:
        messages.pop(0)
    while messages[0]["role"] != "user":
        messages.pop(0)
    return messages


class FollowupModal(discord.ui.Modal, title="Continue local conversation"):
    question = discord.ui.TextInput(label="Question", style=discord.TextStyle.paragraph, max_length=2000)

    def __init__(self, view):
        super().__init__(timeout=300)
        self.owner_view = view
        self.revision = view.revision
        self.question.label = "Question (visible in this channel)" if not view.private else "Private question"

    async def on_submit(self, interaction):
        if not await self.owner_view.interaction_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self.owner_view.generate(str(self.question), interaction, expected_revision=self.revision)


class LocalChatView(OwnerView):
    def __init__(self, *, private=True, channel_id=None, authorize=None, **kwargs):
        super().__init__(timeout=600, **kwargs)
        self.private = private
        self.channel_id = channel_id
        self.authorize = authorize
        self.history = []
        self.message = None
        self.active_request = None
        self.cancel_requested = None
        self.closed = False
        self.revision = 0
        self.rebuild()

    async def interaction_check(self, interaction):
        allowed = (not self.closed and not self.is_finished()
                   and interaction.guild_id == self.guild_id and interaction.user.id == self.actor_id
                   and (self.channel_id is None or interaction.channel_id == self.channel_id)
                   and await (self.authorize(interaction.user) if self.authorize else self.bot.is_owner(interaction.user)))
        if not allowed:
            await interaction.response.send_message("This conversation belongs to its requester, or your access has expired. Open `$ask` again.", ephemeral=True)
        return allowed

    def destination_matches(self):
        if self.message is None:
            return False
        message_guild = getattr(getattr(self.message, "guild", None), "id", None)
        return (message_guild == self.guild_id
                and (not self.private or message_guild is None)
                and (self.channel_id is None or getattr(getattr(self.message, "channel", None), "id", None) == self.channel_id))

    def rebuild(self):
        self.revision += 1
        self.clear_items()
        disabled = self.closed or self.is_finished()
        cancel = discord.ui.Button(label="Cancel generation", style=discord.ButtonStyle.danger,
                                   disabled=disabled or self.active_request is None)
        cancel.callback = partial(self.cancel, request_id=self.active_request, revision=self.revision)
        self.add_item(cancel)
        follow = discord.ui.Button(label="Ask a follow-up", style=discord.ButtonStyle.primary,
                                   disabled=disabled or self.active_request is not None)
        follow.callback = partial(self.follow, revision=self.revision)
        self.add_item(follow)
        fresh = discord.ui.Button(label="New topic", disabled=disabled or self.active_request is not None)
        fresh.callback = partial(self.fresh, revision=self.revision)
        self.add_item(fresh)

    async def edit_response(self, **kwargs):
        if not self.destination_matches():
            return False
        try:
            await self.message.edit(allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        except discord.HTTPException:
            return False
        return True

    def current(self, identity):
        return not self.closed and not self.is_finished() and self.active_request == identity

    async def generate(self, prompt, interaction=None, *, expected_revision=None):
        async with self.lock:
            if (self.active_request or self.closed or self.is_finished()
                    or expected_revision is not None and expected_revision != self.revision):
                if interaction:
                    await interaction.followup.send("This conversation is busy or expired. Open a current question control.", ephemeral=True)
                return
            if not self.destination_matches():
                return  # Never infer or fall back into a different destination.
            try:
                messages = append_prompt(self.history, prompt)
            except LocalAIError as exc:
                if interaction:
                    await interaction.followup.send(str(exc), ephemeral=True)
                else:
                    await self.edit_response(content=str(exc), view=self)
                return
            identity = self.active_request = str(uuid.uuid4())
            self.cancel_requested = None
            self.rebuild()
        try:
            async with self.lock:
                if not self.current(identity):
                    return
                if not await self.edit_response(content="Thinking locally...", embed=None, view=self):
                    return
            async with self.lock:
                if not self.current(identity):
                    return
            result = await self.service.chat(identity, messages)
            async with self.lock:
                if not self.current(identity):
                    return
                if self.cancel_requested == identity:
                    await self.edit_response(content="Response discarded after cancellation.", embed=None)
                    return
                self.history = messages + [{"role": "assistant", "content": result["text"]}]
                embed = discord.Embed(title="Local conversation", description=result["text"][:3900])
                if not self.private:
                    embed.add_field(name="Question", value=str(prompt)[:1000], inline=False)
                embed.set_footer(text="Llama 3.2 3B. No notes or web searched; no tools or actions. This conversation expires after 10 minutes.")
                await self.edit_response(content=None, embed=embed)
        except LocalAIError as exc:
            async with self.lock:
                if self.current(identity):
                    await self.edit_response(content=str(exc), embed=None)
        except asyncio.CancelledError:
            try:
                await self.service.cancel(identity)
            except LocalAIError:
                pass
            raise
        finally:
            async with self.lock:
                if self.active_request == identity:
                    self.active_request = None
                    self.cancel_requested = None
                    self.rebuild()
                    if not self.closed and not self.is_finished():
                        await self.edit_response(view=self)

    async def cancel(self, interaction, *, request_id=_CURRENT, revision=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            identity = self.active_request if request_id is _CURRENT else request_id
            if (not self.current(identity) or identity is None
                    or revision is not None and revision != self.revision):
                await interaction.response.send_message("That generation has already finished or changed.", ephemeral=True)
                return
            self.cancel_requested = identity
        await interaction.response.defer(ephemeral=True)
        try:
            await self.service.cancel(identity)
            text = "Cancellation requested."
        except LocalAIError as exc:
            text = str(exc)
        await interaction.followup.send(text, ephemeral=True)

    async def follow(self, interaction, *, revision=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished() or self.active_request or revision is not None and revision != self.revision:
                await interaction.response.send_message("This control is busy or expired. Use the current conversation.", ephemeral=True)
                return
            await interaction.response.send_modal(FollowupModal(self))

    async def fresh(self, interaction, *, revision=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished() or self.active_request or revision is not None and revision != self.revision:
                await interaction.response.send_message("This control is busy or expired. Use the current conversation.", ephemeral=True)
                return
            self.history = []
            await interaction.response.send_modal(FollowupModal(self))

    async def on_timeout(self):
        async with self.lock:
            identity = self.active_request
            self.closed = True
            self.active_request = None
            self.cancel_requested = None
            self.history = []
            self.stop()
            self.rebuild()
            await self.edit_response(view=None)
        if identity:
            try:
                await self.service.cancel(identity)
            except LocalAIError:
                pass
