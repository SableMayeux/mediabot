# AI chat, web search, and desktop GPU sharing

MediaBot can answer locally, search current public sources, and prefer a larger
model on an optional Windows desktop. These are separate capabilities. Turning
the desktop on does not grant anyone access to it.

## Use it in Discord

```text
$ask explain why RAID 0 needs backups
$ask --desktop explain the tradeoffs in detail
$ask --server a quick local question
$ask --web worthwhile Nintendo eShop deals in the US
$ask --desktop --web worthwhile Nintendo eShop deals in the US
$ask --web --private what changed in Home Assistant this month?
```

An ordinary server request answers in its original channel. In a DM it answers
in that DM. `--private` sends the answer to your DM when the command starts in a
server. All flags go before the question, in any order. `--desktop` requires the
desktop model and never falls back to the server. `--server` uses only the server.
Choose one or omit both for automatic selection. The flags do not grant access
or turn the desktop on. If a required desktop is unavailable, the bot reports
why and makes no server request.

Web answers include source IDs such as `[S1]`, clickable source cards, and the
retrieval time. Cards distinguish page text from a search-engine excerpt. The
footer identifies the model and whether the desktop GPU, server GPU, or server CPU answered.
The original question remains readable, and long answers continue in replies.

The follow-up button retains GPU selection and web mode. Start a new `$ask` to
change either; **New topic** clears history while keeping both settings.
Each web follow-up searches only its new
question, so include the subject again: "Which of those Nintendo games supports
local co-op?" is more useful than "Which ones?" The short conversation history
goes to the local model, not the search engines. Context expires ten minutes after the answer finishes; Discord messages remain.
Cancel covers search, queued requests, and generation.

Requests wait in a shared FIFO queue with a visible position and automatic delivery.
The bot admits up to 20 requests, with at most two per user including their active
request. Waiting expires after one hour. Queue contents and conversation history
are in memory, so a bot restart clears them. Access is checked again before inference.
Only a definite gateway busy rejection is retried; a timeout or interrupted
generation is never silently replayed.

During server generation, **Finish sooner** stops the long pass and asks the same
model for a compact, complete answer without extended thinking. It keeps the
original question and web evidence, and can still take time to finish. It does
not publish a chopped-off draft. The desktop worker does not support this control.
Use **Cancel generation** to discard the request instead.

`--private` controls Discord delivery. Web search still sends the current query
to upstream search engines and retrieves public pages. It does not search Life
notes, files, Discord channels, or Home Assistant, and it cannot execute actions.
Sources can be incomplete or wrong. Citations make an answer checkable, not
guaranteed correct. Search now examines up to eight results, ranks readable and
relevant evidence, removes near-duplicates, and can follow two relevant links
one level deeper on the same sites. It accepts documents up to 2 MiB, while the
final model evidence stays bounded to three excerpts and 6,000 UTF-8 bytes.
JavaScript-only catalogs may still omit product data from the retrieved HTML.
An aggregator's price is not automatically a verified price in your region or
at a particular retailer; the answer should identify that distinction.

## Choose who can use each capability

The bot owner can run these commands in an allowed server or directly in DM:

```text
$admin ai status
$admin ai access @Jenificial
$admin ai allow @Jenificial desktop
$admin ai deny @Jenificial web
$admin ai reset @Jenificial web
```

Use a Discord user ID when a mention is unavailable in DM. With several allowed
servers, select the relevant server using `$admin server <server ID>` first.

| Capability | Default for current allowed-server members | Meaning |
| --- | --- | --- |
| `server` | Allowed | Use the server model, including fallback. |
| `desktop` | Denied | Use the optional desktop model while it is available. |
| `web` | Allowed | Retrieve public web evidence for `$ask --web`. Also needs an allowed AI backend. |

The owner retains all three capabilities. Overrides persist in MediaBot's
existing database, independently of desktop power, sleep, and bot restarts.
Current server membership remains required. Follow-ups recheck permissions,
and a result is withheld if its required access was revoked during generation.
Server permission also controls `$recommend --auto`; ordinary provider recommendations remain available.
These permissions do not grant Life access or change media-account links.

With automatic selection, desktop is preferred when the requester has desktop
access and its worker is ready. If unavailable, server fallback requires server
access, and the reply footer explains the fallback. A desktop-only
user receives an availability message instead. Once the desktop accepts a
request, a disconnect is reported rather than rerunning it on another GPU.
`$ask --desktop` enforces desktop-only for that conversation even when the
requester also has server access. It does not change their saved permissions.

## Server quality and playback switch

The bot owner can use the same commands in DM or the allowed server:

```text
$admin ai quality on
$admin ai quality off
$admin ai quality
$ask --server explain the tradeoff
```

**On** is the default. Server conversation uses Huihui's Qwen3 8B v2
Q4_K_M with reasoning enabled. Its 16,384-token budget includes reasoning,
with a 25-minute deadline and an 8,192-token context window. This community variant reduces refusals;
it is not a guarantee of accuracy or an answer to every possible prompt.
The model partially offloads to the GPU, with an explicit 24-layer limit
on the reference 8 GiB RTX 4060 configuration.

**Off** selects standard Qwen3 4B Instruct 2507 Q4_K_M on the CPU,
leaving GPU memory for playback. Switching off cancels an active server GPU
answer and unloads its model. It does not retry that answer. Ask again to
use the smaller model. The selection persists across gateway and bot restarts.

Even with quality on, new server requests use the CPU model while the GPU
guard detects media work or insufficient headroom. A GPU answer already in
progress is interrupted if playback starts. Both runtimes unload after each
request. The CPU runtime has no GPU devices, is limited to four CPU cores with low scheduling priority and
4 GiB RAM, and still requires a fresh monitor and sufficient host RAM.
CPU answers can take longer, especially with conversation history or web
evidence. The CPU budget is 4,096 output tokens with a 20-minute deadline.
Both modes aim to finish the requested explanation; no fixed 200-word instruction
is applied. These are maximum budgets, not target response lengths. Ordinary
questions can finish much sooner. A resource interruption still stops generation.
A token-limited answer is explicitly marked incomplete, never presented as complete.

The smaller model is the standard instruction model, not an ablated variant.
It retains its upstream behavior. Reducing refusals can also damage factual
reliability, so the quality switch is a resource and model choice, not an accuracy guarantee.
Current prices and events should use `--web` with checkable sources.

The switch applies to server requests and does not change anyone's access.
Use `--server` to select this path explicitly. Automatic desktop selection,
when enabled and permitted, is separate. Structured task extraction continues
using its existing Llama model and validators; the conversation switch does
not give either model tools or control of the host.

## Desktop switch

Open the installed **MediaBot Desktop AI** shortcut. Its window offers On, Off,
and current status. On enables a private Tailscale HTTPS endpoint and allows
the model to load on demand. Off removes that endpoint and stops this worker's
owned model processes. Other applications and Ollama installations are not
selected for termination. There is no automatic startup entry.

For scripts, the installed switch also accepts:

```powershell
& "$env:LOCALAPPDATA\MediaBot\DesktopAI\Desktop-AI.ps1" -Action On
& "$env:LOCALAPPDATA\MediaBot\DesktopAI\Desktop-AI.ps1" -Action Status
& "$env:LOCALAPPDATA\MediaBot\DesktopAI\Desktop-AI.ps1" -Action Off
```

The worker uses the pinned Gemma 4 12B QAT model with reasoning enabled, an
8,192-token context, and a 4,096-token generation budget including reasoning.
It unloads after an idle interval. Its VRAM, temperature, and host-memory checks
can make it temporarily unavailable. Cold loading takes longer than a warm
reply. The two GPUs serve separate requests; they do not pool VRAM.

## Install the optional infrastructure

The normal installer remains a Discord/media setup. AI infrastructure is an
explicit additional deployment, not enabled by changing a model name.

1. Provision the bounded server gateway and its GPU monitor from
   [`deploy/local-ai`](../deploy/local-ai/README.md). MediaBot accepts the gateway
   protocol, not an unrestricted Ollama API URL.
2. Start the private search stack using
   [`deploy/web-search/README.md`](../deploy/web-search/README.md). Connect the bot
   to `mediabot_search_frontend` and set `WEB_SEARCH_URL=http://web-search:8080`.
3. For the desktop worker, use the Windows installer in
   [`deploy/desktop-ai`](../deploy/desktop-ai/README.md). It requires an existing
   signed Ollama executable, the exact reviewed model cache, Python, and an
   authenticated Tailscale installation. It does not download weights for you.
4. Copy only the desktop gateway token over an authenticated administrator
   channel to the server gateway's read-only token mount. Set `DESKTOP_AI_URL`
   to that desktop's private Tailscale HTTPS endpoint. Keep it out of Git and
   Discord. The bot itself does not receive the desktop token.
5. Verify ordinary chat, cited web answers, desktop On/Off, cancellation, and
   an account without desktop access. `$admin ai status` shows backend readiness
   and whether web search is configured; individual searches report provider
   failures directly.

If a dispatched desktop request stops, the paused response reports a bounded
reason such as a generation deadline, memory reserve, runtime error, or broken
connection. It is not silently rerun on another GPU. Desktop worker Status also
retains a sanitized last-failure code and UTC timestamp until restart. A failed
Ollama status poll is separate from the physical GPU/RAM monitor and does not
terminate otherwise safe inference.

The server retains its media-priority GPU interlock. Structured task extraction
and recommendation classification continue using their existing server profile
and validators. Web evidence expands only the conversational context, and
neither backend gains task or home-control tools.
