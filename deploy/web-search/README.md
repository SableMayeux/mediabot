# Optional private web search

This stack runs SearXNG without a host port, account, subscription, or GPU. MediaBot sends only the explicit search question. Conversation history and Life notes are not search parameters. Search engines receive the question and the server's outgoing IP; source sites receive ordinary page requests. Do not include secrets in a web question.

The official image is pinned to the multi-architecture manifest observed on 2026-09-12: `sha256:1f3720f59a0361592dc942e4be50e0ebb63c112d915883fcb78a011995fffb47`. Its amd64 child is `sha256:4d5f763fc369ec448be6e7ca1464275ee5d50851dbfd38edb29694bc87f027c2`, version `2026.9.12-d4f00d15d`. Updating it requires an explicit digest change and a search smoke test.

## Start

Run in `deploy/web-search` on the Docker host. Generate the secret once, keeping the existing file on subsequent runs:

```sh
umask 077
test -e .env || python3 -c 'import pathlib,secrets; pathlib.Path(".env").write_text("SEARXNG_SECRET="+secrets.token_hex(32)+"\n")'
docker compose config --quiet
docker compose up -d
docker compose ps
```

Connect the MediaBot container to the existing `mediabot_search_frontend` network through its Compose configuration. Add the external network declaration below and include `web_search` in the bot service's networks:

```yaml
networks:
  web_search:
    external: true
    name: mediabot_search_frontend
```

Set `WEB_SEARCH_URL=http://web-search:8080` in MediaBot's environment, then recreate the bot using its normal deployment procedure. Keep its existing outbound network so it can fetch public source pages. The search container has its own outbound network and does not join the local model network. No reverse proxy, router forwarding, or public search endpoint is needed. Loopback `http://127.0.0.1:11889` is accepted by the Python client for isolated operator tests, but this Compose file publishes no loopback port either.

The image runs as UID/GID 977 with a read-only root, a 512 MiB memory ceiling, one CPU, and bounded temporary cache. Search request URLs are absent from Granian access logs because access logging is disabled; the bot sends searches using POST and does not persist query text in this service. Do not enable debug logging on an instance processing personal questions. Settings use three general engines and enable the JSON API. Some engines can be throttled or blocked by CAPTCHA, so successful container health does not prove a successful search.

## Verify from MediaBot

```sh
docker compose exec mediabot python -c 'import asyncio; from mediabot.services.web_search import WebSearchService; exec("async def run():\n s=WebSearchService()\n try:\n  sources=await s.search(\"Home Assistant official documentation\")\n  print([{k:v for k,v in x.items() if k != \"text\"} for x in sources])\n finally:\n  await s.close()\nasyncio.run(run())")'
```

Expect one to three public sources with IDs, URLs, UTC retrieval timestamps, and `page` or `snippet` provenance. A snippet means the search result was available but a page could not be read. It is not a claim that the whole page was inspected. Treat dates/prices in these texts as claims to verify against the source, not as guaranteed-current facts.

The service reads at most the first five search results and returns at most three sources. Each source has at most 2,000 UTF-8 bytes of text, for 6,000 total. HTML extraction favors the main/article body and selects passages near the question's words with surrounding context. Excerpts preserve source words, and nonadjacent passages have an explicit `[...]` separator. Page requests accept text/HTML only, no JavaScript, downloads, cookies, ambient proxy, or stored credentials. DNS answers must all be globally routable and are pinned into each connection; every redirect is revalidated. Private/LAN/loopback/link-local/overlay addresses and IPv6 transition forms are rejected. Both response sizes and deadlines are bounded. Retrieved text is untrusted evidence, and the model must not treat it as tool authorization or instructions.

Disable with an empty `WEB_SEARCH_URL` and recreate MediaBot. Stop only this stack with `docker compose stop`; this does not alter the bot database, Life data, VPN, or other services.

Upstream references: [container installation](https://docs.searxng.org/admin/installation-docker.html), [search API](https://docs.searxng.org/dev/search_api.html), [JSON format configuration](https://docs.searxng.org/admin/settings/settings_search.html), [pinned upstream container source](https://github.com/searxng/searxng/blob/d4f00d15d4c2b8260124a9039b80bc9c1c26499b/container/dist.dockerfile).
