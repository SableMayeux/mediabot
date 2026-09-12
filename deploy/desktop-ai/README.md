# Optional Windows desktop GPU

This worker lets MediaBot use a Windows desktop GPU when its owner turns it on.
It is optional. Access permissions are enforced by MediaBot independently of the
desktop switch. Turning the desktop on does not grant anyone permission to use it.

The first supported configuration is a 16 GB NVIDIA GPU, Python 3.10 or newer,
signed portable Ollama 0.34.0, and the pinned `gemma4:12b-it-qat` model. The
installer reuses an existing runtime and model cache. It does not download a
model, modify another Ollama installation, or enable the worker at login.

## Install

Run PowerShell as the desktop user. Supply the actual paths to the verified
portable executable and the model cache containing Gemma 4 12B QAT.

```powershell
.\Install-DesktopAI.ps1 `
    -OllamaExe 'C:\AI\ollama\ollama.exe' `
    -ModelsPath 'C:\AI\models'
```

The installer creates **MediaBot Desktop AI** on the desktop. Open it for
**Turn on**, **Turn off**, and **Refresh**. Closing the switch window leaves its
current setting in effect. It starts off after installation and after reboot.
The command interface is available too:

```powershell
& "$env:LOCALAPPDATA\MediaBot\DesktopAI\Desktop-AI.ps1" -Action On
& "$env:LOCALAPPDATA\MediaBot\DesktopAI\Desktop-AI.ps1" -Action Status
& "$env:LOCALAPPDATA\MediaBot\DesktopAI\Desktop-AI.ps1" -Action Off
```

ON enables the private endpoint, but loads the model only when a request arrives.
The model unloads after 60 idle seconds. OFF cancels active work, closes the
owned Windows process job, and removes the worker's dedicated Tailscale Serve
route. The next request may use the server if that user has server access.
An interrupted answer is not silently replayed to another model.

## Server connection

The gateway binds only `127.0.0.1:11890`. Ollama binds only `127.0.0.1:11440`.
Tailscale Serve exposes `https://<desktop-tailnet-name>:8445` privately within
the tailnet. This does not use Funnel or add a public listener. The worker
refuses to overwrite an unrelated Serve target on port 8445.

The installer prints the endpoint and credential **path**, never the credential.
Copy `gateway-token.txt` securely into the server gateway's dedicated desktop
credential file. Keep it out of environment dumps, command lines, commits,
Discord messages, and screenshots. The local installation directory permits
only the desktop user and SYSTEM. The bot must call its existing server gateway,
which applies the permitted backend selection before forwarding requests here.

Authenticated endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/status` | Read readiness, fixed model identity, and resource state |
| `POST /v1/chat` | One bounded conversation, optionally with source evidence |
| `POST /v1/cancel` | Cancel the exact request UUID, with a cancellation receipt |

Only conversation requests are accepted. Structured task classification stays
on the server. Search is performed upstream; this worker does not fetch pages
or expose actions. It receives at most three source excerpts, retains their
trusted metadata for the response, and asks the model to cite `[S1]`, `[S2]`,
and `[S3]`. Source content stays outside the system instruction role. This helps
separate data from instructions but does not make model answers infallible.

## Resource behavior

Admission requires 10 GiB free GPU memory and 8 GiB free system RAM before cold
load. A loaded model can accept requests while at least 2 GiB GPU memory and
4 GiB system RAM remain free. New requests yield when GPU utilization is at
least 75 percent. A running request stops if remaining memory falls below those
reserves, temperature reaches 85 C, or resource monitoring fails.

The worker uses an 8192-token context, a 4096-token generation budget including
reasoning, and a 120-second request deadline. It serializes requests. Model
weights and extra context still compete with games and desktop applications;
use OFF before gaming when you want the GPU back immediately.

Runtime and manifest hashes are checked at startup. The exact model manifest is
`sha256:38044be4f923e5a55264ed7df4eaac2676651a905f735197c504045140c02bd3`.
The gateway also checks local blob presence and sizes and the serving runtime's
model digest. It never silently pulls a missing or updated model.

If ON fails, inspect `gateway-errors.log` in the installation directory and run
Status. Logs do not record prompts, source excerpts, or credentials. An
unavailable worker is not a reason to change GPU bindings, expose Ollama, or
disable the media server's existing safety controls.
