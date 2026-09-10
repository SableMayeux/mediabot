"""Private Discord review controls for the narrow torrent gateway."""

from __future__ import annotations

import asyncio
import contextlib
import io
import math
from typing import Any

import discord

from mediabot.services.torrent_intake import TorrentIntakeError, TorrentInputError


def size_text(value: Any) -> str:
    try:
        value = float(value)
    except (ValueError, TypeError):
        return "metadata pending"
    if not math.isfinite(value) or value < 0:
        return "metadata pending"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return "metadata pending"


def safe_text(value: Any, length: int = 180) -> str:
    return discord.utils.escape_markdown(
        discord.utils.escape_mentions(str(value).replace("\x00", "")[:length])
    )[:length]


async def review_role(bot, user) -> str | None:
    if await bot.is_owner(user):
        return "owner"
    if getattr(getattr(user, "guild_permissions", None), "administrator", False):
        return "admin"
    return None


class TorrentReviewView(discord.ui.View):
    """One actor-bound private session; approval always revalidates on server."""

    def __init__(self, *, bot, service, actor_id: int, guild_id: int, role: str):
        super().__init__(timeout=300)
        self.bot, self.service = bot, service
        self.actor_id, self.guild_id, self.role = actor_id, guild_id, role
        self.status: dict[str, Any] = {}
        self.items: list[dict[str, Any]] = []
        self.selected: set[int] = set()
        self.page = 0
        self.queue_page = 0
        self.interaction = None
        self.poll_task = None
        self.action_lock = asyncio.Lock()
        self.receipt_sent = False
        self.closing = False

    async def api(self, action, **fields):
        return await self.service.review_request(
            action, actor_id=self.actor_id, actor_role=self.role, **fields,
        )

    async def interaction_check(self, interaction):
        role = await review_role(self.bot, interaction.user)
        if (interaction.user.id != self.actor_id or interaction.guild_id != self.guild_id
                or role is None or (self.role == "owner" and role != "owner")):
            await interaction.response.send_message("This private review belongs to its authorized reviewer.", ephemeral=True)
            return False
        self.role = role
        return True

    def absorb(self, status):
        if not isinstance(status, dict) or not status.get("id"):
            raise TorrentIntakeError("The gateway returned an invalid review session.")
        changed = status.get("manifest_sha256") != self.status.get("manifest_sha256")
        self.status = status
        if changed or status.get("phase") == "approved":
            self.selected = {int(i) for i in status.get("selected_indexes", [])}
            self.page = 0

    async def load(self, info_hash=None):
        if info_hash:
            self.absorb(await self.api("open", info_hash=info_hash))
        else:
            payload = await self.api("list")
            self.items = payload.get("items", [])
        self.rebuild()

    def rows(self):
        return self.status.get("manifest") or []

    def embed(self):
        if not self.status:
            return discord.Embed(
                title="Torrent review", color=discord.Color.blue(),
                description=("Choose a request to inspect its files and scan limits."
                             if self.items else "No manual requests are waiting."),
            )
        state = self.status
        rows = self.rows()
        chosen = [r for r in rows if int(r["index"]) in self.selected]
        embed = discord.Embed(
            title=safe_text(state.get("name") or "Torrent review", 180),
            description=safe_text(state.get("detail") or state.get("phase", "Pending"), 1000),
            color=discord.Color.orange(),
        )
        embed.add_field(name="Category / state", value=f"{safe_text(state.get('category', 'manual'))} / {safe_text(state.get('phase', 'pending'))}", inline=False)
        if rows:
            total = sum(int(r.get("size", 0)) for r in chosen)
            limit = int(state.get("scan_limit_bytes", 95 * 1024 * 1024))
            skipped = sum(int(r.get("size", 0)) for r in chosen if int(r.get("size", 0)) > limit)
            embed.add_field(name="Selected download", value=f"{len(chosen)} of {len(rows)} files, {size_text(total)}", inline=False)
            page = rows[self.page * 20:(self.page + 1) * 20]
            lines = [f"{'[x]' if int(r['index']) in self.selected else '[ ]'} {safe_text(r.get('name', ''), 100)} ({size_text(r.get('size'))})" for r in page]
            chunks, chunk = [], ""
            for line in lines:
                if len(chunk) + len(line) + 1 > 1024:
                    chunks.append(chunk)
                    chunk = ""
                chunk += ("\n" if chunk else "") + line
            if chunk:
                chunks.append(chunk)
            for index, chunk in enumerate(chunks):
                embed.add_field(name=(f"Files, page {self.page + 1}/{max(1, math.ceil(len(rows) / 20))}" if index == 0 else "Files continued"), value=chunk, inline=False)
            embed.add_field(name="Scan coverage", value=(
                f"Files above {size_text(limit)} are skipped: {size_text(skipped)} selected bytes. "
                "Archive contents may have additional limits. A partial scan permits seeding in manual quarantine; "
                "it does not establish that a file is safe to run."
            ), inline=False)
        else:
            embed.add_field(name="Size", value="Metadata pending", inline=False)
        reasons = state.get("reason_codes") or []
        if reasons:
            embed.add_field(name="Guard reasons", value=safe_text(", ".join(reasons), 600), inline=False)
        scan = state.get("scan_result")
        if isinstance(scan, dict):
            embed.add_field(name="Scan result", value=safe_text(scan.get("detail") or scan.get("status") or str(scan), 600), inline=False)
        if state.get("receipt"):
            embed.add_field(name="Download approval", value="Recorded and verified by the review service. Refresh for download and scan progress.", inline=False)
        if state.get("live_state"):
            embed.add_field(name="Live progress", value=(
                f"{safe_text(state['live_state'])}; downloaded {size_text(state.get('downloaded_bytes'))}; "
                f"uploaded {size_text(state.get('uploaded_bytes'))}. "
                f"Scan: {safe_text(state.get('scan_status') or 'pending')}; "
                f"scanned {size_text(state.get('scanned_bytes'))}, unscanned {size_text(state.get('unscanned_bytes'))}."
            ), inline=False)
        if state.get("live_status_error"):
            embed.add_field(name="Current status unavailable", value=safe_text(state["live_status_error"], 500), inline=False)
        embed.set_footer(text="Private controls expire after 5 minutes. Reopen with $torrent review.")
        return embed

    def button(self, label, callback, *, style=discord.ButtonStyle.secondary, disabled=False, row=None):
        item = discord.ui.Button(label=label, style=style, disabled=disabled, row=row)
        item.callback = callback
        self.add_item(item)

    def rebuild(self):
        self.clear_items()
        if not self.status:
            page = self.items[self.queue_page * 25:(self.queue_page + 1) * 25]
            if page:
                options = [discord.SelectOption(label=str(t.get("name") or "Manual request")[:100], value=t["info_hash"], description=f"{t.get('category', 'manual')} / {t.get('state', 'pending')}"[:100]) for t in page]
                picker = discord.ui.Select(placeholder="Choose request", options=options)
                async def choose(interaction):
                    await self.perform(interaction, "open", info_hash=picker.values[0])
                picker.callback = choose
                self.add_item(picker)
            self.button("Previous", self.queue_previous, disabled=self.queue_page == 0)
            self.button("Next", self.queue_next, disabled=(self.queue_page + 1) * 25 >= len(self.items))
            self.button("Close", self.close_review)
            return
        rows = self.rows()
        phase = self.status.get("phase", "")
        ready = phase == "ready"
        page = rows[self.page * 20:(self.page + 1) * 20]
        selectable = [r for r in page if r.get("selectable", True)]
        if selectable:
            menu_session = self.status.get("id")
            menu_manifest = self.status.get("manifest_sha256")
            picker = discord.ui.Select(
                placeholder="Selected files on this page", min_values=0, max_values=len(selectable),
                disabled=not ready, row=0,
                options=[discord.SelectOption(label=str(r.get("name", "File"))[-100:], value=str(r["index"]), default=int(r["index"]) in self.selected, description=size_text(r.get("size"))) for r in selectable],
            )
            async def select(interaction):
                await interaction.response.defer()
                async with self.action_lock:
                    if (self.status.get("phase") != "ready"
                            or self.status.get("id") != menu_session
                            or self.status.get("manifest_sha256") != menu_manifest):
                        await interaction.followup.send("This review is no longer accepting file changes. Refresh its current state.", ephemeral=True)
                        return
                    self.selected.difference_update(int(r["index"]) for r in selectable)
                    self.selected.update(int(v) for v in picker.values)
                    await self.render()
            picker.callback = select
            self.add_item(picker)
        self.button("Previous files", self.previous, disabled=self.page == 0, row=1)
        self.button("Next files", self.next, disabled=(self.page + 1) * 20 >= len(rows), row=1)
        self.button("Refresh", self.refresh, row=1)
        approval_snapshot = (self.status.get("id"), self.status.get("manifest_sha256"), tuple(sorted(self.selected)))
        async def approve_displayed(interaction):
            await self.perform(interaction, "approve", displayed=approval_snapshot)
        self.button("Approve selected download", approve_displayed, style=discord.ButtonStyle.success, disabled=not ready or not self.selected, row=2)
        self.button("Retry metadata", self.retry, disabled=phase not in {"timeout", "failed", "cancelled", "expired", "interrupted"}, row=2)
        if self.status.get("recoverable_hold") and self.role == "owner":
            self.button("Recover hold", self.recover, style=discord.ButtonStyle.danger, row=2)
        self.button("Cancel review", self.close_review, row=3)
        self.button("Full file list", self.manifest_file, disabled=not rows, row=3)

    async def render(self):
        self.rebuild()
        if self.interaction:
            await self.interaction.edit_original_response(embed=self.embed(), view=self)

    async def perform(self, interaction, action, **fields):
        await interaction.response.defer()
        async with self.action_lock:
            try:
                if action == "approve":
                    displayed = fields.pop("displayed", None)
                    current = (self.status.get("id"), self.status.get("manifest_sha256"), tuple(sorted(self.selected)))
                    if self.status.get("phase") != "ready" or displayed != current:
                        raise TorrentInputError("The displayed review or selection changed. Inspect the current file list before approving.")
                    fields = {"manifest_sha256": self.status.get("manifest_sha256"), "selected_indexes": sorted(self.selected)}
                if action != "open":
                    fields["review_id"] = self.status["id"]
                self.absorb(await self.api(action, **fields))
                await self.render()
                if action == "approve" and self.status.get("receipt") and not self.receipt_sent:
                    # A durable DM contains only the useful approval receipt, no magnet.
                    # If DMs are closed, the gateway still retains the authoritative receipt.
                    try:
                        await interaction.user.send(
                            f"Torrent download approved: {safe_text(self.status.get('name', 'manual request'))}. "
                            f"{len(self.selected)} selected files. Content remains in manual quarantine. "
                            "Use `$torrent review` for scan and seeding status.",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        self.receipt_sent = True
                    except discord.HTTPException:
                        await interaction.followup.send("Approval is saved. I could not DM the receipt; reopen `$torrent review` to inspect it.", ephemeral=True)
                self.ensure_polling()
            except (TorrentIntakeError, TorrentInputError) as exc:
                await interaction.followup.send(str(exc), ephemeral=True)

    def ensure_polling(self):
        if self.status.get("phase") in {"metadata", "metadata_pending", "starting", "bootstrapping"}:
            if self.poll_task is None or self.poll_task.done():
                self.poll_task = asyncio.create_task(self.poll())

    async def poll(self):
        try:
            for _ in range(55):
                await asyncio.sleep(3)
                async with self.action_lock:
                    self.absorb(await self.api("status", review_id=self.status["id"]))
                    await self.render()
                    if self.closing and self.status.get("phase") == "cancelled":
                        self.stop()
                        await self.interaction.delete_original_response()
                        return
                    if self.status.get("phase") not in {"metadata", "metadata_pending", "starting", "bootstrapping"}:
                        return
        except (TorrentIntakeError, discord.HTTPException):
            if self.interaction:
                with contextlib.suppress(discord.HTTPException):
                    await self.interaction.followup.send("Progress updates stopped. Use Refresh to check the gate's current state.", ephemeral=True)

    async def refresh(self, interaction):
        await self.perform(interaction, "status")

    async def manifest_file(self, interaction):
        async with self.action_lock:
            lines = ["Reviewed file selection (approval is a separate action).",
                     "Large files may be skipped by the scanner; approval does not mean safe to execute.", ""]
            for item in self.rows():
                lines.append(f"{'SELECTED' if int(item['index']) in self.selected else 'SKIPPED'} | {int(item.get('size', 0))} bytes | {item.get('name', '')}")
            data = io.BytesIO("\n".join(lines).encode("utf-8"))
            await interaction.response.send_message(
                file=discord.File(data, filename="reviewed-file-list.txt"), ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def retry(self, interaction):
        await self.perform(interaction, "retry")

    async def recover(self, interaction):
        await self.perform(interaction, "recover")

    async def approve(self, interaction):
        await self.perform(interaction, "approve", displayed=(self.status.get("id"), self.status.get("manifest_sha256"), tuple(sorted(self.selected))))

    async def previous(self, interaction):
        self.page = max(0, self.page - 1)
        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def next(self, interaction):
        self.page = min(max(0, math.ceil(len(self.rows()) / 20) - 1), self.page + 1)
        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def queue_previous(self, interaction):
        self.queue_page = max(0, self.queue_page - 1)
        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def queue_next(self, interaction):
        self.queue_page = min(max(0, math.ceil(len(self.items) / 25) - 1), self.queue_page + 1)
        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def close_review(self, interaction):
        await interaction.response.defer()
        async with self.action_lock:
            if self.status and not self.status.get("receipt"):
                try:
                    self.absorb(await self.api("cancel", review_id=self.status["id"]))
                except (TorrentIntakeError, TorrentInputError) as exc:
                    await interaction.followup.send(str(exc), ephemeral=True)
                    return
                if self.status.get("phase") == "metadata":
                    self.closing = True
                    await self.render()
                    self.ensure_polling()
                    return
            if self.poll_task:
                self.poll_task.cancel()
            self.stop()
            await self.interaction.delete_original_response()

    async def on_timeout(self):
        if self.poll_task:
            self.poll_task.cancel()
        async with self.action_lock:
            if self.status and not self.status.get("receipt"):
                with contextlib.suppress(TorrentIntakeError, TorrentInputError):
                    await self.api("cancel", review_id=self.status["id"])
            if self.interaction:
                with contextlib.suppress(discord.HTTPException):
                    await self.interaction.delete_original_response()


class TorrentReviewLauncher(discord.ui.View):
    """Public, hash-free launcher; all names and manifests stay private."""

    def __init__(self, *, bot, service, guild_id, info_hash=None):
        super().__init__(timeout=300)
        self.bot, self.service, self.guild_id, self.info_hash = bot, service, guild_id, info_hash
        self.message = None

    async def on_timeout(self):
        if self.message:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=None)

    @discord.ui.button(label="Review privately", style=discord.ButtonStyle.primary)
    async def launch(self, interaction, button):
        role = await review_role(self.bot, interaction.user)
        if interaction.guild_id != self.guild_id or role is None:
            await interaction.response.send_message("An owner or administrator must review this download.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        view = TorrentReviewView(bot=self.bot, service=self.service, actor_id=interaction.user.id, guild_id=self.guild_id, role=role)
        view.interaction = interaction
        try:
            await view.load(self.info_hash)
            await interaction.edit_original_response(embed=view.embed(), view=view)
            view.ensure_polling()
        except (TorrentIntakeError, TorrentInputError) as exc:
            await interaction.edit_original_response(content=str(exc), view=None)
