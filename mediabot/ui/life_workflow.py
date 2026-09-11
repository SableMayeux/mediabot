"""Private, explicit capture promotion and task completion through Nextcloud."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import re
import uuid
from zoneinfo import ZoneInfo

import discord

from mediabot.services.life_workflow import LifeWorkflowError


LOCAL_ZONE = os.getenv("LIFE_TIMEZONE", "America/Denver")
NEXTCLOUD_ORIGIN = os.getenv("NEXTCLOUD_PUBLIC_ORIGIN", "http://10.0.0.53:8082").rstrip("/")


def safe(value, maximum=180):
    return discord.utils.escape_markdown(" ".join(str(value or "").split())[:maximum])


def parse_local_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    if "T" not in text and " " not in text:
        raise ValueError("Include a date and time, such as 2026-09-12 14:30.")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Use YYYY-MM-DD HH:MM, or an ISO date/time with an explicit UTC offset.") from exc
    if parsed.tzinfo is None:
        zone = ZoneInfo(LOCAL_ZONE)
        candidates = []
        for fold in (0, 1):
            candidate = parsed.replace(tzinfo=zone, fold=fold)
            if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == parsed:
                candidates.append(candidate)
        if not candidates:
            raise ValueError("That local time does not exist because of daylight saving. Choose another time.")
        if len({item.utcoffset() for item in candidates}) > 1:
            raise ValueError("That time occurs twice. Include its UTC offset, for example -06:00 or -07:00.")
        parsed = candidates[0]
    return parsed.isoformat(timespec="seconds")


def time_label(value):
    if not value:
        return "None"
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.fromisoformat(value).date().isoformat()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.strftime("%Y-%m-%d %H:%M") + " (time zone unspecified)"
        return parsed.astimezone(ZoneInfo(LOCAL_ZONE)).strftime("%Y-%m-%d %H:%M %Z")
    except (TypeError, ValueError):
        return "Unavailable"


class OwnerView(discord.ui.View):
    def __init__(self, *, bot, service, actor_id, guild_id, timeout=300):
        super().__init__(timeout=timeout)
        self.bot, self.service = bot, service
        self.actor_id, self.guild_id = actor_id, guild_id
        self.lock = asyncio.Lock()

    async def interaction_check(self, interaction):
        allowed = (not self.is_finished() and interaction.user.id == self.actor_id
                   and interaction.guild_id == self.guild_id and await self.bot.is_owner(interaction.user))
        if not allowed:
            await interaction.response.send_message("This private Life view is unavailable to this account or has expired. Open `$life` again.", ephemeral=True)
        return allowed

    def options(self):
        return {"bot": self.bot, "service": self.service, "actor_id": self.actor_id, "guild_id": self.guild_id}


class ProposalView(OwnerView):
    def __init__(self, *, action, fields, **kwargs):
        super().__init__(**kwargs)
        self.action = action
        self.fields = dict(fields)
        self.fields.setdefault("request_id", str(uuid.uuid4()))
        self.completed = False
        label = "Create event" if action == "create_event" else "Complete task" if action == "complete_task" else "Create task"
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.success)
        button.callback = self.confirm
        self.add_item(button)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self.cancel
        self.add_item(cancel)

    def embed(self):
        title = "Confirm calendar event" if self.action == "create_event" else "Confirm task completion" if self.action == "complete_task" else "Confirm Nextcloud task"
        embed = discord.Embed(title=title, description=safe(self.fields.get("title", "Selected task"), 200))
        if self.action == "create_event":
            embed.add_field(name="Starts", value=time_label(self.fields["start_at"]))
            embed.add_field(name="Ends", value=time_label(self.fields["end_at"]))
            reminder = self.fields.get("reminder_minutes")
            embed.add_field(name="Nextcloud reminder", value="None" if reminder is None else f"{reminder} minute(s) before")
        elif self.action == "create_task":
            embed.add_field(name="Due", value=time_label(self.fields.get("due_at")))
            remind_at = self.fields.get("remind_at")
            embed.add_field(name="Reminder", value=("One Nextcloud notification at " + time_label(remind_at)) if remind_at else "None. A due date is not an alert.", inline=False)
        embed.set_footer(text="Nextcloud is authoritative. The original capture stays unchanged.")
        return embed

    async def confirm(self, interaction):
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        async with self.lock:
            if self.completed or self.is_finished():
                await interaction.followup.send("This proposal is already closed.", ephemeral=True)
                return
            fields = {k: v for k, v in self.fields.items() if self.action != "complete_task" or k != "title"}
            try:
                result = await self.service.request(self.action, actor_id=self.actor_id, **fields)
            except LifeWorkflowError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            self.completed = True
            for item in self.children:
                item.disabled = True
            entity = result.get("event", result.get("task", {}))
            text = "Event created" if self.action == "create_event" else "Task completed" if self.action == "complete_task" else "Task created"
            embed = discord.Embed(title=text, description=safe(entity.get("title", self.fields.get("title")), 200))
            embed.add_field(name="Receipt", value=f"`{self.fields['request_id']}`", inline=False)
            embed.set_footer(text="Saved in Nextcloud. Open $life to refresh its current state.")
            await interaction.edit_original_response(content=None, embed=embed, view=self)
            self.stop()

    async def cancel(self, interaction):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.completed or self.is_finished():
                await interaction.response.send_message("This proposal is already closed.", ephemeral=True)
                return
            self.stop()
            await interaction.response.edit_message(content="Proposal cancelled. No new action was submitted.", embed=None, view=None)


class PromotionModal(discord.ui.Modal):
    def __init__(self, *, owner_view, capture, event=False):
        super().__init__(title="Plan calendar event" if event else "Make a Nextcloud task", timeout=300)
        self.owner_view, self.capture, self.event = owner_view, dict(capture), event
        self.name = discord.ui.TextInput(label="Title", default=str(capture.get("title", ""))[:200], max_length=200)
        self.add_item(self.name)
        if event:
            self.start = discord.ui.TextInput(label=f"Start ({LOCAL_ZONE})"[:45], placeholder="YYYY-MM-DD HH:MM", max_length=40)
            self.end = discord.ui.TextInput(label=f"End ({LOCAL_ZONE})"[:45], placeholder="YYYY-MM-DD HH:MM", max_length=40)
            self.reminder = discord.ui.TextInput(label="Reminder minutes before (optional)", required=False, placeholder="30", max_length=5)
            for item in (self.start, self.end, self.reminder):
                self.add_item(item)
        else:
            self.due = discord.ui.TextInput(label=f"Due ({LOCAL_ZONE}, optional)"[:45], placeholder="YYYY-MM-DD HH:MM", required=False, max_length=40)
            self.add_item(self.due)
            self.remind = discord.ui.TextInput(label=f"Remind ({LOCAL_ZONE}, optional)"[:45], placeholder="YYYY-MM-DD HH:MM", required=False, max_length=40)
            self.add_item(self.remind)

    async def on_submit(self, interaction):
        if not await self.owner_view.interaction_check(interaction):
            return
        fields = {"capture_id": self.capture["id"], "title": str(self.name).strip()}
        try:
            if not fields["title"]:
                raise ValueError("Give the action a title.")
            if self.event:
                fields.update(start_at=parse_local_time(str(self.start)), end_at=parse_local_time(str(self.end)))
                if not fields["start_at"] or not fields["end_at"] or datetime.fromisoformat(fields["end_at"]) <= datetime.fromisoformat(fields["start_at"]):
                    raise ValueError("The event needs an end time after its start time.")
                text = str(self.reminder).strip()
                if text and (not text.isdecimal() or not 0 <= int(text) <= 10080):
                    raise ValueError("Reminder must be 0 to 10080 minutes before the event.")
                fields["reminder_minutes"] = int(text) if text else None
            else:
                fields["due_at"] = parse_local_time(str(self.due))
                fields["remind_at"] = parse_local_time(str(self.remind))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        proposal = ProposalView(action="create_event" if self.event else "create_task", fields=fields, **self.owner_view.options())
        await interaction.response.send_message(embed=proposal.embed(), view=proposal, ephemeral=True)


class LifeView(OwnerView):
    PAGE_SIZE = 20

    def __init__(self, *, capture=None, **kwargs):
        super().__init__(**kwargs)
        self.mode, self.items, self.page, self.selected = "captures", [capture] if capture else [], 0, capture
        self.truncated = False
        self.rebuild()

    def embed(self):
        embed = discord.Embed(title="Life inbox" if self.mode == "captures" else "Nextcloud tasks")
        if self.selected:
            embed.description = safe(self.selected.get("title"), 200)
            if self.mode == "tasks":
                embed.add_field(name="Status", value=safe(self.selected.get("status"), 40) or "Unknown")
                embed.add_field(name="Due", value=time_label(self.selected.get("due_at")))
                reminder = self.selected.get("reminder")
                if reminder:
                    embed.add_field(name="Reminder", value=safe(reminder.get("status"), 40) + " - " + time_label(reminder.get("at")), inline=False)
                if self.selected.get("recurring"):
                    embed.add_field(name="Recurring task", value="Complete or edit this in Nextcloud to preserve recurrence.", inline=False)
            else:
                embed.add_field(name="Captured", value=time_label(self.selected.get("created_at")))
                embed.add_field(name="Choose deliberately", value="Keep it as a note, make a task, or plan an event. Nothing moves or changes until you confirm.", inline=False)
        else:
            embed.description = "Choose an item below." if self.items else "No items in this view. Capture with `$think` first, or refresh Tasks."
        embed.set_footer(text=f"Page {self.page + 1} of {max(1, (len(self.items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)}. " + ("Showing a bounded list; use Nextcloud for older items." if self.truncated else "Owner-only. Original thoughts are preserved."))
        return embed

    async def load(self, mode):
        result = await self.service.request(mode, actor_id=self.actor_id, limit=50)
        self.mode, self.items = mode, result["items"]
        self.truncated = bool(result.get("truncated"))
        self.page, self.selected = 0, None
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        self.add_item(discord.ui.Button(label="Open Nextcloud", url=NEXTCLOUD_ORIGIN + "/index.php/apps/" + ("tasks/" if self.mode == "tasks" else "notes/"), row=4))
        for mode, label in (("captures", "Inbox"), ("tasks", "Tasks")):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary if self.mode == mode else discord.ButtonStyle.secondary, row=0)
            async def change(interaction, target=mode):
                if not await self.interaction_check(interaction):
                    return
                await interaction.response.defer(ephemeral=True)
                async with self.lock:
                    try:
                        await self.load(target)
                    except LifeWorkflowError as exc:
                        await interaction.followup.send(str(exc), ephemeral=True)
                        return
                    await interaction.edit_original_response(embed=self.embed(), view=self)
            button.callback = change
            self.add_item(button)
        page_items = self.items[self.page * self.PAGE_SIZE:(self.page + 1) * self.PAGE_SIZE]
        if page_items:
            menu = discord.ui.Select(placeholder="Choose a capture" if self.mode == "captures" else "Choose a task", options=[discord.SelectOption(label=" ".join(str(item.get("title", "Untitled")).split())[:100] or "Untitled", value=str(index), default=item is self.selected) for index, item in enumerate(page_items)], row=1)
            snapshot = tuple(page_items)
            async def select(interaction):
                if not await self.interaction_check(interaction):
                    return
                async with self.lock:
                    if tuple(self.items[self.page * self.PAGE_SIZE:(self.page + 1) * self.PAGE_SIZE]) != snapshot:
                        await interaction.response.send_message("This menu is stale. Use the current view.", ephemeral=True)
                        return
                    try:
                        index = int(menu.values[0])
                        if index < 0 or index >= len(snapshot):
                            raise ValueError()
                        self.selected = snapshot[index]
                    except (ValueError, IndexError):
                        await interaction.response.send_message("Choose an item from this page.", ephemeral=True)
                        return
                    self.rebuild()
                    await interaction.response.edit_message(embed=self.embed(), view=self)
            menu.callback = select
            self.add_item(menu)
        for delta, label in ((-1, "Previous"), (1, "Next")):
            button = discord.ui.Button(label=label, row=2, disabled=not 0 <= self.page + delta < (len(self.items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
            async def page(interaction, offset=delta):
                if not await self.interaction_check(interaction):
                    return
                async with self.lock:
                    self.page = max(0, min(self.page + offset, max(0, (len(self.items) - 1) // self.PAGE_SIZE)))
                    self.rebuild()
                    await interaction.response.edit_message(embed=self.embed(), view=self)
            button.callback = page
            self.add_item(button)
        if self.selected and self.mode == "captures":
            capture = dict(self.selected)
            for event, label in ((False, "Make task"), (True, "Plan event")):
                button = discord.ui.Button(label=label, style=discord.ButtonStyle.success, row=3)
                async def promote(interaction, is_event=event, chosen=capture):
                    if not await self.interaction_check(interaction):
                        return
                    if self.mode != "captures" or not self.selected or self.selected.get("id") != chosen.get("id"):
                        await interaction.response.send_message("The selection changed. Use its current action button.", ephemeral=True)
                        return
                    await interaction.response.send_modal(PromotionModal(owner_view=self, capture=chosen, event=is_event))
                button.callback = promote
                self.add_item(button)
        elif self.selected and self.mode == "tasks" and self.selected.get("status") != "COMPLETED" and not self.selected.get("recurring"):
            task = dict(self.selected)
            button = discord.ui.Button(label="Mark complete", style=discord.ButtonStyle.success, row=3)
            async def complete(interaction):
                if not await self.interaction_check(interaction):
                    return
                if self.mode != "tasks" or self.selected != task:
                    await interaction.response.send_message("The task selection changed. Refresh before completing it.", ephemeral=True)
                    return
                proposal = ProposalView(action="complete_task", fields={"task_id": task["id"], "etag": task["etag"], "title": task["title"]}, **self.options())
                await interaction.response.send_message(embed=proposal.embed(), view=proposal, ephemeral=True)
            button.callback = complete
            self.add_item(button)


class LifeLauncher(OwnerView):
    def __init__(self, *, capture=None, initial="captures", **kwargs):
        super().__init__(timeout=300, **kwargs)
        self.capture, self.initial = capture, initial
        button = discord.ui.Button(label="Open Life privately", style=discord.ButtonStyle.primary)
        button.callback = self.open
        self.add_item(button)

    async def open(self, interaction):
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        view = LifeView(capture=self.capture, **self.options())
        try:
            if not self.capture:
                await view.load(self.initial)
        except LifeWorkflowError as exc:
            view.stop()
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)
