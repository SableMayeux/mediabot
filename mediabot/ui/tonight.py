"""One movie-pick card with requester filters and personal sharing controls."""
import asyncio
import contextlib
import logging

import discord
from discord.ext.commands import CheckFailure

from mediabot.services.group_picks import parse_runtime, resolve_tonight_genres

logger = logging.getLogger('mediabot')


class TonightFiltersModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title='Choose movie filters')
        self.picks_view = view
        self.genres = discord.ui.TextInput(label='Genres (blank means any)', default=', '.join(view.genres)[:600], required=False, max_length=600)
        self.runtime = discord.ui.TextInput(label='Maximum runtime: minutes, 2h or 1h30m', default=str(view.max_minutes), max_length=20)
        self.add_item(self.genres)
        self.add_item(self.runtime)

    async def on_submit(self, interaction):
        if not await self.picks_view.guard(interaction, owner=True):
            return
        try:
            genres = resolve_tonight_genres(self.genres.value, self.picks_view.available_genres)
            minutes = parse_runtime(self.runtime.value)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await self.picks_view.update(interaction, genres=genres, max_minutes=minutes)

    async def on_error(self, interaction, error):
        await self.picks_view.on_error(interaction, error, self.runtime)


class TonightRuntimeModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title='Maximum movie runtime')
        self.picks_view = view
        self.runtime = discord.ui.TextInput(label='Minutes, 2h or 1h30m', default=str(view.max_minutes), max_length=20)
        self.add_item(self.runtime)

    async def on_submit(self, interaction):
        if not await self.picks_view.guard(interaction, owner=True):
            return
        try:
            minutes = parse_runtime(self.runtime.value)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await self.picks_view.update(interaction, max_minutes=minutes)

    async def on_error(self, interaction, error):
        await self.picks_view.on_error(interaction, error, self.runtime)


class TonightView(discord.ui.View):
    def __init__(self, *, actor_id, guild_id, available_genres, genres, max_minutes,
                 authorize, render, set_consent, watch_url):
        super().__init__(timeout=600)
        self.actor_id, self.guild_id = actor_id, guild_id
        self.available_genres = tuple(available_genres)
        self.genres, self.max_minutes = tuple(genres), max_minutes
        self.authorize, self.render, self.set_consent, self.watch_url = authorize, render, set_consent, watch_url
        self.action_lock = asyncio.Lock()
        self.message = None
        self.rebuild([])

    async def guard(self, interaction, *, owner=False):
        if self.is_finished():
            await interaction.response.send_message('These controls expired. Run `$tonight` for a fresh card.', ephemeral=True)
            return False
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message('Open `$tonight` in this server or your own DM.', ephemeral=True)
            return False
        if owner and interaction.user.id != self.actor_id:
            await interaction.response.send_message('These filters belong to the requester. Run `$tonight` for your own picks. You can still change your own rating sharing here.', ephemeral=True)
            return False
        try:
            await self.authorize(interaction.user)
        except CheckFailure as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return False
        return True

    def rebuild(self, picks):
        self.clear_items()
        # Keep requested genres visible even if they are outside the first page
        # of library genre names or have no current matches.
        names = list(dict.fromkeys((*self.genres, *self.available_genres)))[:24]
        options = [discord.SelectOption(label='Any genre', value='any', default=not self.genres)]
        options += [discord.SelectOption(label=name[:100], value=str(index), default=name in self.genres)
                    for index, name in enumerate(names)]
        genres = discord.ui.Select(placeholder='Choose genres (match any selected)', options=options,
                                   min_values=1, max_values=min(5, len(options)), row=0)
        async def choose_genres(interaction):
            if not await self.guard(interaction, owner=True):
                return
            selected = tuple(names[int(value)] for value in genres.values if value != 'any')
            await self.update(interaction, genres=selected)
        genres.callback = choose_genres
        self.add_item(genres)
        durations = sorted({30, 60, 90, 120, 150, 180, 240, 300, self.max_minutes})
        runtime = discord.ui.Select(placeholder='Maximum runtime', row=1, options=[
            discord.SelectOption(label=f'Up to {minutes} minutes', value=str(minutes), default=minutes == self.max_minutes)
            for minutes in durations] + [discord.SelectOption(label='Custom runtime...', value='custom')])
        async def choose_runtime(interaction):
            if not await self.guard(interaction, owner=True):
                return
            if runtime.values[0] == 'custom':
                await interaction.response.send_modal(TonightRuntimeModal(self))
            else:
                await self.update(interaction, max_minutes=parse_runtime(runtime.values[0]))
        runtime.callback = choose_runtime
        self.add_item(runtime)
        for label, enabled in (('Include my ratings', True), ('Keep my ratings private', False)):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary, row=2)
            async def consent(interaction, enabled=enabled):
                if not await self.guard(interaction):
                    return
                await interaction.response.defer(ephemeral=True, thinking=True)
                async with self.action_lock:
                    if self.is_finished():
                        raise CheckFailure('These controls expired. Run `$tonight` for a fresh card.')
                    await self.authorize(interaction.user)
                    await self.set_consent(interaction.user.id, enabled)
                    try:
                        payload, refreshed = await self.render(self.genres, self.max_minutes)
                        self.rebuild(refreshed)
                        if self.message:
                            await self.message.edit(**payload, view=self)
                    except Exception as error:
                        logger.warning('Tonight refresh after consent failed (%s)', type(error).__name__)
                        await interaction.followup.send('Your sharing choice was saved, but the movie card could not refresh. Run `$tonight` for fresh picks.', ephemeral=True)
                        return
                text = ('You joined these picks and enabled group rating sharing. Only your own preference changed; your individual ratings are not posted.'
                        if enabled else 'Group rating sharing is off. You left this group unless you are its requester; your ratings still inform your own picks.')
                await interaction.followup.send(text, ephemeral=True)
            button.callback = consent
            self.add_item(button)
        refresh = discord.ui.Button(label='Update picks', row=2)
        async def update_picks(interaction):
            if await self.guard(interaction, owner=True):
                await self.update(interaction)
        refresh.callback = update_picks
        self.add_item(refresh)
        custom = discord.ui.Button(label='Custom filters', row=2)
        async def custom_filters(interaction):
            if await self.guard(interaction, owner=True):
                await interaction.response.send_modal(TonightFiltersModal(self))
        custom.callback = custom_filters
        self.add_item(custom)
        for index, item in enumerate(picks, 1):
            self.add_item(discord.ui.Button(label=f'Watch pick {index}', url=self.watch_url(item['Id']), row=3))

    async def update(self, interaction, *, genres=None, max_minutes=None):
        await interaction.response.defer()
        async with self.action_lock:
            if self.is_finished():
                raise CheckFailure('These controls expired. Run `$tonight` for a fresh card.')
            await self.authorize(interaction.user)
            wanted = self.genres if genres is None else tuple(genres)
            minutes = self.max_minutes if max_minutes is None else max_minutes
            payload, picks = await self.render(wanted, minutes)
            self.genres, self.max_minutes = wanted, minutes
            self.rebuild(picks)
            await interaction.edit_original_response(**payload, view=self)

    async def on_timeout(self):
        for item in self.children:
            if not getattr(item, 'url', None):
                item.disabled = True
        if self.message:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)

    async def on_error(self, interaction, error, item):
        if not isinstance(error, CheckFailure):
            logger.error('Tonight action failed (%s)', type(error).__name__)
        text = str(error) if isinstance(error, CheckFailure) else 'Those picks could not update. Run `$tonight` for a fresh card.'
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
