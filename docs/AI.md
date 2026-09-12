# AI chat, web search, and desktop GPU sharing

MediaBot can answer locally, search current public sources, and prefer a larger
model on an optional Windows desktop. These are separate capabilities. Turning
the desktop on does not grant anyone access to it.

## Use it in Discord

```text
$ask explain why RAID 0 needs backups
$ask --web worthwhile Nintendo eShop deals in the US
$ask --web --private what changed in Home Assistant this month?
```

An ordinary server request answers in its original channel. In a DM it answers
in that DM. `--private` sends the answer to your DM when the command starts in a
server. Both flags go before the question, in either order.

Web answers include source IDs such as `[S1]`, clickable source cards, and the
retrieval time. Cards distinguish page text from a search-engine excerpt. The
footer identifies the model and whether the desktop or server GPU answered.
The original question remains readable, and long answers continue in replies.

The follow-up button retains web mode. Each follow-up searches only its new
question, so include the subject again: "Which of those Nintendo games supports
local co-op?" is more useful than "Which ones?" The short conversation history
goes to the local model, not the search engines. Context expires after ten
minutes; Discord messages remain. Cancel covers both search and generation.

`--private` controls Discord delivery. Web search still sends the current query
to upstream search engines and retrieves public pages. It does not search Life
notes, files, Discord channels, or Home Assistant, and it cannot execute actions.
Sources can be incomplete or wrong. Citations make an answer checkable, not
guaranteed correct. JavaScript-only sites may provide only search excerpts.

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
These permissions do not grant Life access or change media-account links.

Desktop is preferred when the requester has desktop access and its worker is
ready. If unavailable, server fallback requires server access. A desktop-only
user receives an availability message instead. Once the desktop accepts a
request, a disconnect is reported rather than rerunning it on another GPU.

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

The server retains its media-priority GPU interlock. Structured task extraction
and recommendation classification continue using their existing server profile
and validators. Web evidence expands only the conversational context, and
neither backend gains task or home-control tools.
