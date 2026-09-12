"""Bounded public-web evidence, never instructions or an action/tool runner."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html.parser import HTMLParser
import ipaddress
import json
import os
import re
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

MAX_QUERY_BYTES = 800
MAX_SOURCES = 3
MAX_TEXT_BYTES = 6000
MAX_PAGE_BYTES = 262144
MAX_SEARCH_BYTES = 262144
MAX_REDIRECTS = 3
USER_AGENT = "MediaBot-WebSearch/1.0 (bounded source retrieval)"


class WebSearchError(RuntimeError):
    """Safe to display to the user; never includes upstream response bodies."""


class UnsafeDestination(WebSearchError):
    pass


class PageUnavailable(WebSearchError):
    pass


def clip_utf8(value, budget):
    return value.encode("utf-8")[:budget].decode("utf-8", errors="ignore")


def validate_endpoint(value):
    text = str(value or "")
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        if (text != text.strip() or any(ord(char) < 33 for char in text)
                or parsed.scheme != "http" or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or (parsed.hostname, parsed.port) not in {
                    ("web-search", 8080), ("127.0.0.1", 11889),
                    ("localhost", 11889), ("::1", 11889),
                }):
            raise ValueError
    except ValueError as exc:
        raise WebSearchError("Web search URL must name the configured private search service.") from exc
    return text.rstrip("/")


def _public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if (not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified or address.is_loopback or address.is_link_local):
        return False
    # Reject IPv6 transition encodings, including NAT64, mapped IPv4 and 6to4.
    if isinstance(address, ipaddress.IPv6Address):
        return (address in ipaddress.ip_network("2000::/3")
                and address.sixtofour is None and address.teredo is None)
    return True


def validate_public_url(value):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or value != value.strip() or "\\" in value
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise UnsafeDestination("A search result has an unsafe destination.")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 443 if parsed.scheme == "https" else 80}
                or "%" in host or host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".onion"))):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
                raise ValueError
        else:
            if not _public_ip(host):
                raise ValueError
        authority = "[" + host + "]" if ":" in host else host
        return urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError) as exc:
        raise UnsafeDestination("A search result has an unsafe destination.") from exc


async def _resolve_public(host, port):
    try:
        rows = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP), timeout=2)
    except (OSError, asyncio.TimeoutError) as exc:
        raise PageUnavailable("A source could not be resolved.") from exc
    if not rows or any(not _public_ip(row[4][0]) for row in rows):
        raise UnsafeDestination("A search result resolves outside the public internet.")
    return list(dict.fromkeys((row[0], row[4][0]) for row in rows))


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """The connector uses exactly the DNS answers we validated, with original TLS SNI."""

    def __init__(self, host, addresses):
        self.host = host
        self.addresses = addresses

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host != self.host or not self.addresses or any(not _public_ip(ip) for _, ip in self.addresses):
            raise UnsafeDestination("The source connection does not match its validated destination.")
        return [{"hostname": host, "host": ip, "port": port, "family": fam,
                 "proto": socket.IPPROTO_TCP, "flags": socket.AI_NUMERICHOST}
                for fam, ip in self.addresses]

    async def close(self):
        pass


class _TextParser(HTMLParser):
    IGNORED = {"script", "style", "head", "noscript", "nav", "footer", "form", "svg", "iframe", "object", "template"}
    BREAKS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "article", "section", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = []
        self.parts = []
        self.main_parts = []
        self.article_parts = []
        self.main_depth = 0
        self.article_depth = 0

    def _append(self, text):
        self.parts.append(text)
        if self.main_depth:
            self.main_parts.append(text)
        if self.article_depth:
            self.article_parts.append(text)

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.main_depth += 1
        if tag == "article":
            self.article_depth += 1
        if tag in self.IGNORED:
            self.ignored.append(tag)
        if not self.ignored and tag in self.BREAKS:
            self._append("\n")

    def handle_endtag(self, tag):
        if tag in self.ignored:
            self.ignored = self.ignored[:len(self.ignored) - 1 - self.ignored[::-1].index(tag)]
        if not self.ignored and tag in self.BREAKS:
            self._append("\n")
        if tag == "main":
            self.main_depth = max(0, self.main_depth - 1)
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)

    def handle_data(self, data):
        if not self.ignored:
            self._append(data)


def extract_text(raw):
    parser = _TextParser()
    parser.feed(raw)
    for parts in (parser.main_parts, parser.article_parts, parser.parts):
        text = "\n".join(filter(None, (" ".join(line.split()) for line in "".join(parts).splitlines())))
        if text:
            return text
    return ""


def select_excerpt(text, query, budget=MAX_TEXT_BYTES // MAX_SOURCES):
    """Keep at most two contiguous passages, preserving neighboring product/context lines."""
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    ignored = {"a", "an", "and", "any", "are", "can", "do", "for", "from", "get", "how",
               "in", "is", "it", "me", "of", "on", "or", "some", "the", "this", "to",
               "was", "what", "when", "where", "which", "who", "with", "would", "you"}
    terms = {word for word in re.findall(r"\w{2,}", query.casefold()) if word not in ignored}
    # Rank bounded paragraph anchors. Repeated boilerplate such as "Nintendo
    # Switch" gets less weight than terms occurring in only a few paragraphs.
    spans = []
    for paragraph in re.finditer(rb"[^\n]+", raw):
        start, end = paragraph.span()
        while end - start > 600:
            cut = raw.rfind(b" ", start + 300, start + 600)
            cut = cut if cut > start else start + 600
            spans.append((start, cut))
            start = cut + 1
        if start < end:
            spans.append((start, end))
    matches = [terms & set(re.findall(r"\w{2,}", raw[start:end].decode("utf-8", errors="ignore").casefold()))
               for start, end in spans]
    frequency = {term: sum(term in words for words in matches) for term in terms}
    ranked = sorted((-sum(1 / frequency[term] ** .5 for term in words), index)
                    for index, words in enumerate(matches) if words)
    if not ranked:
        return clip_utf8(text, budget)
    passage_budget = (budget - len("\n[...]\n")) // 2
    selected = []
    for _, index in ranked:
        anchor_start, anchor_end = spans[index]
        # Keep preceding titles/labels as well as following prices/qualifiers.
        start = max(0, anchor_start - min(240, passage_budget // 3))
        preceding_break = raw.rfind(b"\n", max(0, start - 120), start)
        if preceding_break >= 0:
            start = preceding_break + 1
        end = min(len(raw), start + passage_budget)
        if end < anchor_end:
            start, end = max(0, anchor_end - passage_budget), anchor_end
        if end < len(raw):
            last_break = raw.rfind(b"\n", max(anchor_end, end - 140), end)
            if last_break < 0:
                last_break = raw.rfind(b" ", max(anchor_end, end - 80), end)
            if last_break >= 0:
                end = last_break
        if any(start < other_end and end > other_start for other_start, other_end in selected):
            continue
        selected.append((start, end))
        if len(selected) == 2:
            break
    if len(selected) == 1:
        # A single relevant passage can use the full allowance with no discontinuity.
        start, end = selected[0]
        end = min(len(raw), start + budget)
        if end < len(raw):
            last_break = raw.rfind(b"\n", max(selected[0][1], end - 140), end)
            if last_break >= 0:
                end = last_break
        selected = [(start, end)]
    return "\n[...]\n".join(raw[start:end].decode("utf-8", errors="ignore").strip()
                           for start, end in sorted(selected))


async def _read_bounded(response, limit):
    encoding = response.headers.get("Content-Encoding", "identity").lower()
    if encoding not in {"", "identity"}:
        raise PageUnavailable("A source returned unsupported compressed content.")
    content_length = response.headers.get("Content-Length", "")
    if content_length.isdigit() and int(content_length) > limit:
        raise PageUnavailable("A source exceeds the retrieval size limit.")
    raw = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        raw.extend(chunk)
        if len(raw) > limit:
            raise PageUnavailable("A source exceeds the retrieval size limit.")
    return bytes(raw)


class WebSearchService:
    def __init__(self, *, base_url=None):
        self.base_url = validate_endpoint(base_url if base_url is not None else os.getenv("WEB_SEARCH_URL", ""))
        self.session = None
        self._session_lock = asyncio.Lock()
        self._search_slots = asyncio.Semaphore(2)

    @property
    def enabled(self):
        return bool(self.base_url)

    async def start(self):
        if not self.enabled:
            raise WebSearchError("Web search is not configured on this server yet.")
        async with self._session_lock:
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=12, connect=3), trust_env=False,
                    auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar(),
                    headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})

    async def close(self):
        async with self._session_lock:
            if self.session is not None and not self.session.closed:
                await self.session.close()
            self.session = None

    async def _results(self, query):
        await self.start()
        try:
            # POST keeps the query out of ordinary access-log URLs.
            async with self.session.post(self.base_url + "/search", data={
                "q": query, "format": "json", "categories": "general", "language": "en-US",
            }, allow_redirects=False) as response:
                if response.status != 200:
                    raise WebSearchError("Web search is temporarily unavailable. Try again shortly.")
                raw = await _read_bounded(response, MAX_SEARCH_BYTES)
                body = json.loads(raw)
                if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                    raise ValueError
                return body["results"][:5]
        except WebSearchError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise WebSearchError("Web search did not return usable results. Try again shortly.") from exc

    async def _fetch_page(self, url):
        current = validate_public_url(url)
        for redirects in range(MAX_REDIRECTS + 1):
            parsed = urlsplit(current)
            addresses = await _resolve_public(parsed.hostname, 443 if parsed.scheme == "https" else 80)
            connector = aiohttp.TCPConnector(
                resolver=_PinnedResolver(parsed.hostname, addresses), use_dns_cache=False,
                force_close=True, limit=1)
            async with aiohttp.ClientSession(
                    connector=connector, timeout=aiohttp.ClientTimeout(total=5, connect=3),
                    trust_env=False, auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar(),
                    headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                             "Accept": "text/html,text/plain,application/xhtml+xml"}) as session:
                async with session.get(current, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        if redirects == MAX_REDIRECTS or not response.headers.get("Location"):
                            raise PageUnavailable("A source redirected too many times.")
                        current = validate_public_url(urljoin(current, response.headers["Location"]))
                        continue
                    if response.status != 200:
                        raise PageUnavailable("A source page is unavailable.")
                    mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if mime not in {"text/html", "text/plain", "application/xhtml+xml"}:
                        raise PageUnavailable("A source is not a supported text page.")
                    raw = await _read_bounded(response, MAX_PAGE_BYTES)
                    charset = response.charset or "utf-8"
                    try:
                        decoded = raw.decode(charset, errors="replace")
                    except LookupError:
                        decoded = raw.decode("utf-8", errors="replace")
                    text = extract_text(decoded) if mime != "text/plain" else decoded.strip()
                    if not text:
                        raise PageUnavailable("A source contains no readable text.")
                    return text, current
        raise PageUnavailable("A source redirected too many times.")

    async def _source(self, result, query=""):
        if not isinstance(result, dict):
            return None
        try:
            url = validate_public_url(result.get("url"))
            # Validate even when only a provider snippet is ultimately available.
            parsed = urlsplit(url)
            await _resolve_public(parsed.hostname, 443 if parsed.scheme == "https" else 80)
        except (UnsafeDestination, PageUnavailable):
            return None
        title = extract_text(str(result.get("title") or parsed.hostname))[:180] or parsed.hostname[:180]
        snippet = extract_text(str(result.get("content") or ""))
        try:
            text, final_url = await asyncio.wait_for(self._fetch_page(url), timeout=7)
            kind = "page"
        except UnsafeDestination:
            return None
        except (aiohttp.ClientError, asyncio.TimeoutError, PageUnavailable, UnicodeError, ValueError):
            text, final_url, kind = snippet, url, "snippet"
        if not text.strip():
            return None
        return {"title": title, "url": final_url, "text": select_excerpt(text, query),
                "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind}

    async def search(self, query):
        if (not isinstance(query, str) or not query.strip() or len(query.encode("utf-8")) > MAX_QUERY_BYTES
                or any(ord(char) < 32 and char not in "\n\t" for char in query)):
            raise WebSearchError("Give web search a question of at most 800 UTF-8 bytes.")
        try:
            return await asyncio.wait_for(self._search(query.strip()), timeout=28)
        except asyncio.TimeoutError as exc:
            raise WebSearchError("Web search reached its deadline. Try again shortly.") from exc

    async def _search(self, query):
        async with self._search_slots:
            results = await self._results(query)
            candidates = []
            seen = set()
            for result in results:
                if not isinstance(result, dict):
                    continue
                try:
                    url = validate_public_url(result.get("url"))
                except UnsafeDestination:
                    continue
                if url not in seen:
                    candidates.append(result)
                    seen.add(url)
            found = await asyncio.gather(*(self._source(result, query) for result in candidates))
            sources = []
            seen.clear()
            for source in found:
                if source and source["url"] not in seen:
                    sources.append({"id": "S" + str(len(sources) + 1), **source})
                    seen.add(source["url"])
                if len(sources) == MAX_SOURCES:
                    break
            if not sources:
                raise WebSearchError("No usable public sources came back. Try a more specific search.")
            return sources
