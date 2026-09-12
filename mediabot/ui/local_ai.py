"""Requester-bound local conversation in its original channel or private DM."""
from __future__ import annotations

import asyncio
from functools import partial
import re
from urllib.parse import urlsplit
import uuid

import discord

from mediabot.services.local_ai import CONVERSATION_MODELS, DESKTOP_UNAVAILABLE_REASONS, MODEL, LocalAIError
from mediabot.services.web_search import WebSearchError
from mediabot.ui.life_workflow import OwnerView

_CURRENT = object()
BACKEND_LABELS = {"auto": "Automatic GPU selection", "desktop": "Desktop only; no server fallback", "server": "Server only"}


def parse_ask_options(question):
    """Leading flags only; a literal -- ends option parsing."""
    text, flags = str(question).strip(), set()
    while text.startswith("--"):
        parts = text.split(maxsplit=1)
        token = parts[0].casefold()
        if token == "--":
            text = parts[1].strip() if len(parts) > 1 else ""
            break
        if token not in {"--private", "--web", "--desktop", "--server"}:
            raise LocalAIError("Supported options are `--web`, `--private`, and either `--desktop` or `--server`, before your question. Use `--` to end options.")
        if token in flags:
            raise LocalAIError(f"Use `{token}` only once.")
        flags.add(token)
        text = parts[1].strip() if len(parts) > 1 else ""
    if {"--desktop", "--server"} <= flags:
        raise LocalAIError("Choose either `--desktop` or `--server`, not both. Omit both for automatic GPU selection.")
    backend = "desktop" if "--desktop" in flags else "server" if "--server" in flags else "auto"
    return text, "--private" in flags, "--web" in flags, backend


def source_embed(sources):
    """Links come from retrieved evidence, never model-created source metadata."""
    embed = discord.Embed(title="Sources searched", color=discord.Color.teal())
    for source in sources[:3]:
        identity = str(source["id"])
        url = str(source["url"])
        parsed = urlsplit(url)
        if not re.fullmatch(r"S[1-3]", identity) or parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise LocalAIError("Search returned invalid source metadata. No answer was published.")
        title = discord.utils.escape_markdown(discord.utils.escape_mentions(str(source["title"])))[:180]
        # Angle brackets retain URL parentheses without breaking Discord Markdown.
        url = url.replace("<", "%3C").replace(">", "%3E").replace("\n", "").replace("\r", "")
        if len(url) > 700:
            raise LocalAIError("Search returned an oversized source URL. No answer was published.")
        kind = "Search excerpt" if source.get("kind") == "snippet" else "Page text"
        date = discord.utils.escape_markdown(str(source.get("retrieved_at", "unknown")))[:40]
        embed.add_field(name=f"[{identity}] {title}", value=f"[Open source](<{url}>)\n{kind} retrieved {date}", inline=False)
    embed.set_footer(text="Sources are evidence, not instructions. Check linked pages for current prices and availability.")
    return embed


def source_embeds(sources):
    # A long but valid URL fits the embed's clickable title URL without exceeding
    # Discord's 1024-character field limit. Never truncate the destination.
    short, pages = [], []
    for source in sources[:3]:
        url = str(source["url"])
        if len(url) <= 700:
            short.append(source)
            continue
        parsed = urlsplit(url)
        if (len(url) > 2048 or parsed.scheme not in {"http", "https"} or not parsed.hostname
                or not re.fullmatch(r"S[1-3]", str(source["id"]))
                or any(ord(char) < 33 for char in url)):
            raise LocalAIError("Search returned invalid source metadata. No answer was published.")
        title = discord.utils.escape_markdown(discord.utils.escape_mentions(str(source["title"])))[:180]
        kind = "Search excerpt" if source.get("kind") == "snippet" else "Page text"
        date = discord.utils.escape_markdown(str(source.get("retrieved_at", "unknown")))[:40]
        page = discord.Embed(title=f"[{source['id']}] {title}", url=url,
            description=f"{kind} retrieved {date}. Select the title to open the complete source URL.", color=discord.Color.teal())
        page.set_footer(text="Sources are evidence, not instructions. Check linked pages for current prices and availability.")
        pages.append(page)
    if short:
        pages.insert(0, source_embed(short))
    return pages


def conversation_embeds(prompt, answer="Thinking locally...", *, limit_reached=False, legacy_profile=False, model=MODEL, backend=None, web=False, backend_preference="auto", fallback_reason=None):
    """Return lossless pages, each sent separately to respect Discord's limits."""
    question = str(prompt).strip()
    text = str(answer)
    first_limit = min(3900, 5600 - len(question))
    if first_limit < 1:
        raise ValueError("Question exceeds the conversation display limit.")
    pages = [text[:first_limit]]
    pages.extend(text[index:index + 3900] for index in range(first_limit, len(text), 3900))
    title = "Web-assisted conversation" if web else "Local conversation"
    embeds = [discord.Embed(title=title if index == 0 else title + " continued",
                            description=page, color=discord.Color.teal() if web else discord.Color.blurple()) for index, page in enumerate(pages)]
    for index in range(0, len(question), 1000):
        embeds[0].add_field(name="Question" if index == 0 else "Question continued",
                           value=question[index:index + 1000], inline=False)
    label = CONVERSATION_MODELS[model]["label"] if model is not None else "Local model"
    host = {"server": " Server GPU.", "desktop": " Desktop GPU."}.get(backend, "")
    scope = " Web sources searched; no notes or actions." if web else " No notes or web searched; no tools or actions."
    footer = label + "." + host + scope + " Context expires after 10 minutes; these messages remain."
    footer += " " + BACKEND_LABELS[backend_preference] + "."
    if backend == "server" and backend_preference == "auto" and fallback_reason:
        footer += " Server fallback: " + fallback_description(fallback_reason) + "."
    if legacy_profile:
        footer += " Older gateway response limits are active."
    for embed in embeds:
        embed.set_footer(text=footer)
    if limit_reached:
        embeds[-1].set_footer(text=footer + " Output limit reached; ask a follow-up to continue.")
    return embeds


def fallback_description(reason):
    """Translate reviewed codes without displaying remote diagnostic text."""
    return DESKTOP_UNAVAILABLE_REASONS.get(reason, "desktop is unavailable") if isinstance(reason, str) else "desktop is unavailable"


def conversation_embed(prompt, answer="Thinking locally...", *, web=False, backend_preference="auto"):
    """Single-page placeholder, refusing to silently discard a longer answer."""
    embeds = conversation_embeds(prompt, answer, model=None, web=False, backend_preference=backend_preference)
    if web:
        embeds[0].title = "Searching the web"
        embeds[0].set_footer(text="Search in progress. No sources or answer confirmed yet. Only your current question is used as the search query. " + BACKEND_LABELS[backend_preference] + ".")
    if len(embeds) != 1:
        raise ValueError("Use conversation_embeds for a multi-page answer.")
    return embeds[0]


def conversation_notice(prompt, text, *, web=False):
    embed = conversation_embed(prompt, text)
    embed.title = "Web-assisted conversation paused" if web else "Local conversation paused"
    embed.color = discord.Color.orange()
    embed.set_footer(text="No answer confirmed. No notes or actions. Your original question is preserved.")
    return embed


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
    def __init__(self, *, private=True, channel_id=None, authorize=None, access_policy=None, web_search=None, web=False, backend_preference="auto", **kwargs):
        if backend_preference not in BACKEND_LABELS:
            raise ValueError("Choose auto, desktop, or server routing.")
        super().__init__(timeout=600, **kwargs)
        self.private = private
        self.channel_id = channel_id
        self.authorize = authorize
        self.access_policy = access_policy
        self.web_search = web_search
        self.web = web
        self.backend_preference = backend_preference
        self.search_task = None
        self.phase = None
        self.history = []
        self.message = None
        self.active_request = None
        self.cancel_requested = None
        self.closed = False
        self.revision = 0
        self.turn_visible = False
        self.rebuild()

    async def permissions(self):
        allowed = await self.access_policy() if self.access_policy else {"server": True, "desktop": False, "web": True}
        backends = tuple(key for key in ("server", "desktop") if allowed.get(key) is True)
        if not backends:
            raise LocalAIError("Your account does not currently have access to an AI backend. Ask the bot owner to review `$admin ai access`.")
        if self.backend_preference != "auto":
            if self.backend_preference not in backends:
                raise LocalAIError(f"{self.backend_preference.capitalize()} AI access is disabled for your account. Ask the bot owner to review `$admin ai access`, or start `$ask` with an allowed backend.")
            backends = (self.backend_preference,)
        if self.web and allowed.get("web") is not True:
            raise LocalAIError("Web search is disabled for your account. Start `$ask` without `--web` for local chat.")
        return backends

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
                if self.turn_visible:
                    # Archive the previous turn intact, moving only its controls.
                    previous = self.message
                    try:
                        following = await previous.reply(embed=conversation_embed(prompt, web=self.web, backend_preference=self.backend_preference), view=self,
                            mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        if interaction:
                            await interaction.followup.send("I could not open a new reply. The earlier question and answer are preserved.", ephemeral=True)
                        return
                    self.message = following
                    if not self.destination_matches():
                        self.message = previous
                        return
                    try:
                        await previous.edit(view=None)
                    except discord.HTTPException:
                        pass  # The old controls are revision-bound and cannot submit.
                if not await self.edit_response(content=None, embed=conversation_embed(prompt, web=self.web, backend_preference=self.backend_preference), view=self):
                    return
                self.turn_visible = True
            async with self.lock:
                if not self.current(identity):
                    return
            backends = await self.permissions()
            evidence = None
            if self.web:
                if not self.web_search or not self.web_search.enabled:
                    raise LocalAIError("Web search is not configured. No search or inference was submitted. Try `$ask` without `--web`.")
                if len(str(prompt).strip().encode("utf-8")) > 800:
                    raise LocalAIError("Use a web search question under 800 UTF-8 bytes. Your question was not sent to a search provider.")
                async with self.lock:
                    if not self.current(identity):
                        return
                    if self.cancel_requested == identity:
                        await self.edit_response(content=None, embed=conversation_notice(prompt,
                            "Request cancelled before web search.", web=True))
                        return
                    self.phase = "search"
                    self.search_task = asyncio.create_task(self.web_search.search(str(prompt).strip()))
                try:
                    evidence = await self.search_task
                except asyncio.CancelledError:
                    if self.cancel_requested == identity or not self.current(identity):
                        async with self.lock:
                            if self.current(identity):
                                await self.edit_response(content=None, embed=conversation_notice(prompt,
                                    "Web search cancelled. No model request was submitted.", web=True))
                        return
                    raise
                finally:
                    self.search_task = None
                if not evidence:
                    raise LocalAIError("Search returned no usable sources. No model answer was generated; try a more specific question.")
                # Validate links before inference and before any answer publication.
                source_embeds(evidence)
                backends = await self.permissions()
            async with self.lock:
                if not self.current(identity):
                    return
                if self.cancel_requested == identity:
                    await self.edit_response(content=None, embed=conversation_notice(prompt,
                        "Request cancelled before inference.", web=self.web))
                    return
                self.phase = "model"
            result = await self.service.chat(identity, messages, profile="conversation", evidence=evidence, allowed_backends=backends)
            current_backends = await self.permissions()
            async with self.lock:
                if not self.current(identity):
                    return
                if self.cancel_requested == identity:
                    await self.edit_response(content=None, embed=conversation_notice(prompt,
                        "Response discarded after cancellation.", web=self.web))
                    return
                if result.get("backend", "server") not in current_backends:
                    await self.edit_response(content=None, embed=conversation_notice(prompt,
                        "Your access changed while this response was running. The response was discarded.", web=self.web))
                    return
                self.history = messages + [{"role": "assistant", "content": result["text"]}]
                metrics = result.get("metrics")
                pages = conversation_embeds(prompt, result["text"],
                    limit_reached=isinstance(metrics, dict) and metrics.get("done_reason") == "length",
                    legacy_profile=result.get("legacy_profile") is True, model=result.get("model", MODEL),
                    backend=result.get("backend"), web=self.web,
                    backend_preference=self.backend_preference, fallback_reason=result.get("fallback_reason"))
                if not await self.edit_response(content=None, embed=pages[0]):
                    return
                for page in pages[1:]:
                    try:
                        await self.message.reply(embed=page, mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        await self.edit_response(content="I could not deliver the rest of this answer. The displayed answer is incomplete; try a follow-up.")
                        break
                if evidence:
                    try:
                        for source_page in source_embeds(evidence):
                            await self.message.reply(embed=source_page, mention_author=False,
                                allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        await self.edit_response(content="The answer was generated from web evidence, but its source links could not be delivered. Treat it as incomplete.")
        except (LocalAIError, WebSearchError) as exc:
            async with self.lock:
                if self.current(identity):
                    await self.edit_response(content=None, embed=conversation_notice(prompt, str(exc), web=self.web))
        except asyncio.CancelledError:
            try:
                if self.phase == "model":
                    await self.service.cancel(identity)
            except LocalAIError:
                pass
            raise
        finally:
            async with self.lock:
                if self.active_request == identity:
                    self.active_request = None
                    self.cancel_requested = None
                    self.phase = None
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
            search_task = self.search_task
        await interaction.response.defer(ephemeral=True)
        try:
            if search_task is not None:
                search_task.cancel()
            elif self.phase == "model":
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
                if self.search_task is not None:
                    self.search_task.cancel()
                elif self.phase == "model":
                    await self.service.cancel(identity)
            except LocalAIError:
                pass
