# Bounded local AI gateway

This directory publishes the server gateway and GPU monitor used by the
homelab deployment. It is an operator-managed reference configuration, not a
universal GPU installer. Review host paths, service account, device index,
resource limits, and model/runtime pins before adapting it to another machine.

The fixed authenticated endpoints are `POST /v1/chat`, `POST /v1/cancel`,
`GET /v1/status`, and `GET /v1/health`. Health checks do not depend on an optional
desktop being awake. Status additionally probes that desktop.

The `structured` profile uses the pinned Llama model, 4,096 context, 256 output
tokens, no web evidence, and no desktop routing. `conversation` permits the
fixed server model and optional pinned desktop Gemma model. Server conversation
uses 1,024 output tokens and 4,096 context, or 8,192 with web evidence. Evidence
is limited to three sources and 6,000 UTF-8 bytes of text. Normal message text
remains limited to 3,000 UTF-8 bytes. Source URLs are returned as provenance but
omitted from the model prompt to conserve context.

The bot provides permitted backends in each authenticated conversation request.
The gateway prefers desktop only when permitted and ready, otherwise uses an
allowed server backend. Once desktop inference is attempted, an interruption
does not trigger server replay. Cancellation stays with the selected backend.
No shell, note retrieval, downloads, arbitrary model selection, or action tools
are exposed. Search itself is provided by the bot and private SearXNG service.

## Required layout and configuration

Under an operator-selected local directory, provision:

- `app/gateway.py` and `app/gpu-watch.py` from `bin/`.
- `models/` containing the exact model manifests and blobs in the lock file.
- `gpu-state/`, written continuously by the host GPU monitor.
- `secrets/local_ai_token`, readable only by the operator and required service.
- `secrets/desktop_ai_token`, the optional desktop worker credential. An empty
  regular file may be mounted when desktop routing is disabled.
- `.env` containing `LOCAL_AI_ROOT=/absolute/local/path`, and optionally
  `DESKTOP_AI_URL=https://your-desktop.your-tailnet.ts.net:8445`.

The supplied Compose file uses UID/GID 1000 and GPU device0. Its Ollama image
is pinned and runs on a Docker-internal network. The gateway attaches to a
frontend network and publishes only a loopback operator port11888. Secrets and
models are mounted read-only. Do not expose Ollama directly.

The supplied service files are examples requiring an existing `localai` account
with Docker access and matching directory permissions. Review and install the
monitor before admitting model requests. Missing, stale, or unhealthy monitor
state rejects server inference. The reference policy reserves4GiB of VRAM and
yields to media workloads. Lowering it is not part of enabling web or desktop AI.

Run the boundary tests before commissioning:

```sh
python3 -m unittest discover -s deploy/local-ai/tests -v
```

See [the AI guide](../../docs/AI.md) for Discord commands, permissions, and the
desktop switch. The normal portable MediaBot installation remains usable with
all these optional services disabled.
