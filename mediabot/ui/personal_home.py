"""Actor-bound personal home screen. Existing commands own their workflows."""
import discord


class QueryModal(discord.ui.Modal):
    def __init__(self, home, command, label):
        super().__init__(title=label)
        self.home, self.command = home, command
        self.query = discord.ui.TextInput(label="Search or request number", max_length=200)
        self.add_item(self.query)

    async def on_submit(self, interaction):
        if not await self.home.interaction_check(interaction):
            return
        await interaction.response.defer()
        await self.home.dispatch(self.command, str(self.query.value))


class HomeView(discord.ui.View):
    def __init__(self, actor_id, authorize, dispatch, *, owner=False, review=False, home_control=False, links=None):
        super().__init__(timeout=600)
        self.actor_id, self.authorize, self.dispatch = actor_id, authorize, dispatch
        actions = [("My requests", "requests", None), ("Find something to watch", "discover", "movie --count 3"),
                   ("Request a title", "request", None), ("My ratings", "rate", ""),
                   ("Report a problem", "report", None), ("Check progress", "status", None),
                   ("My downloads", "downloads", ""), ("Group movie picks", "tonight", ""), ("Account", "whoami", "")]
        if review:
            actions.append(("Review downloads", "torrent", "review"))
        if owner:
            actions.append(("My tasks", "life", "tasks"))
            if home_control:
                actions.append(("Show HA on Denny's", "ha", "home"))
        for index, (label, command, query) in enumerate(actions):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary if index < 3 else discord.ButtonStyle.secondary)
            async def callback(interaction, command=command, query=query, label=label):
                if query is None and command != "requests":
                    await interaction.response.send_modal(QueryModal(self, command, label))
                else:
                    await interaction.response.defer()
                    await self.dispatch(command, query or "")
            button.callback = callback
            self.add_item(button)
        for label, url in (links or {}).items():
            if url:
                self.add_item(discord.ui.Button(label=label, url=url))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Open your own home screen with `$home`.", ephemeral=True)
            return False
        try:
            await self.authorize()
        except Exception as exc:
            from discord.ext.commands import CheckFailure
            if not isinstance(exc, CheckFailure):
                raise
            await interaction.response.send_message(str(exc), ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item):
        message = "That action could not finish. Run `$home` for a fresh screen or try the command directly."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
