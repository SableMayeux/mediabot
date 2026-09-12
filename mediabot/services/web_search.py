"""Bounded public-web evidence, never instructions or an action/tool runner."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from difflib import SequenceMatcher
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
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_SEARCH_BYTES = 262144
MAX_REDIRECTS = 3
MAX_RESULTS = 8
MAX_CHILD_PAGES = 2
SEARCH_DEADLINE = 40
USER_AGENT = "MediaBot-WebSearch/1.0 (bounded source retrieval)"
PRICE = re.compile(r"(?:[$\u00a3\u20ac]\s*\d[\d,.]*|(?:USD|EUR|GBP)\s+\d[\d,.]*)")
DISCOUNT = re.compile(r"^-?\d{1,3}%\s*(?:off)?$", re.I)
SHOPPING_TERMS = frozenset("deal deals price prices sale sales discount discounts offer offers cost buy shopping cheapest".split())

QUERY_FILLER = frozenset("""a an and any are can could do does for from get has have how
    i if in is it its me my of on or our please some that the their there these this to
    was what when where which who will with would you your check see find look tell
    going necessary drill down locale site website right now currently today""".split())


def query_terms(query):
    return {word for word in re.findall(r"\w{2,}", query.casefold()) if word not in QUERY_FILLER}


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
        self.links = []
        self.anchor = None
        self.text_size = 0

    def _append(self, text):
        self.parts.append(text)
        self.text_size += len(text)
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
        if tag == "a" and not self.ignored:
            self._finish_anchor()
            href = dict(attrs).get("href")
            if href and len(href) <= 2048 and len(self.links) < 256:
                self.anchor = [href, "", self.text_size]

    def _finish_anchor(self):
        if self.anchor is not None:
            href, label, start = self.anchor
            label = " ".join(label.split())[:240]
            if label:
                self.links.append({"href": href, "text": label, "_start": start, "_end": self.text_size})
            self.anchor = None

    def handle_endtag(self, tag):
        if tag == "a":
            self._finish_anchor()
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
            if self.anchor is not None:
                self.anchor[1] = (self.anchor[1] + data)[:480]


def extract_document(raw):
    parser = _TextParser()
    parser.feed(raw)
    parser._finish_anchor()
    full = "".join(parser.parts)
    links = []
    for index, link in enumerate(parser.links):
        end = parser.links[index + 1]["_start"] if index + 1 < len(parser.links) else len(full)
        context = " ".join(full[link["_end"]:min(end, link["_end"] + 360)].split())
        links.append({"href": link["href"], "text": link["text"], "context": context})
    for parts in (parser.main_parts, parser.article_parts, parser.parts):
        text = "\n".join(filter(None, (" ".join(line.split()) for line in "".join(parts).splitlines())))
        if text:
            return text, links
    return "", links


def extract_text(raw):
    return extract_document(raw)[0]


def _catalog_excerpt(text, query, budget):
    """Recognize repeated simple offer cards and retain whole contiguous cards."""
    if not (query_terms(query) & SHOPPING_TERMS):
        return None
    lines = text.splitlines()
    discounts = [i for i, line in enumerate(lines) if DISCOUNT.fullmatch(line.strip())]
    price_rows = [i for i, line in enumerate(lines) if PRICE.search(line)
                  and not re.sub(r"[\s,:;/()-]|regular|original|sale|price|was|now", "", PRICE.sub("", line), flags=re.I)]
    if len(discounts) < 3 or not price_rows:
        return None
    blocks = []
    if discounts[0] < price_rows[0]:
        # Discount, title, optional publisher/platform, then old/new prices.
        for start, end in zip(discounts, discounts[1:] + [len(lines)]):
            block = "\n".join(lines[start:end])
            if 3 <= end - start <= 10 and len(PRICE.findall(block)) >= 2 and len(block) < 700:
                blocks.append((start, end))
    else:
        # Title, adjacent old/new prices, discount, and optional expiry/qualifiers.
        starts = [i for i in price_rows if i == 0 or i - 1 not in price_rows]
        for index, prices_start in enumerate(starts):
            prices_end = prices_start + 1
            while prices_end in price_rows:
                prices_end += 1
            if (prices_start == 0 or prices_end >= len(lines)
                    or not DISCOUNT.fullmatch(lines[prices_end].strip())):
                continue
            start = prices_start - 1
            next_start = starts[index + 1] - 1 if index + 1 < len(starts) else len(lines)
            end = min(next_start, prices_end + 4)
            block = "\n".join(lines[start:end])
            if len(PRICE.findall(block)) >= 2 and len(block) < 700:
                blocks.append((start, end))
    if len(blocks) < 3:
        return None
    # Keep the page's introduction, including region caveats, and one run of
    # complete cards. Never join a title to orphaned prices across an omission.
    first = blocks[0][0]
    if len("\n".join(lines[:first]).encode("utf-8")) > budget // 2:
        return None
    end = first
    count = 0
    for start, candidate_end in blocks:
        if start != end or len(("\n".join(lines[:candidate_end]) + "\n[...]").encode("utf-8")) > budget:
            break
        end, count = candidate_end, count + 1
    if count < 2:
        return None
    return "\n".join(lines[:end]) + ("\n[...]" if end < len(lines) else "")


def select_excerpt(text, query, budget=MAX_TEXT_BYTES // MAX_SOURCES):
    """Keep at most two contiguous passages, preserving neighboring product/context lines."""
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    catalog = _catalog_excerpt(text, query, budget)
    if catalog is not None:
        return catalog
    terms = query_terms(query)
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


def _source_score(source, query):
    """Prefer readable, relevant evidence; numbers alone never establish truth."""
    text = source["text"]
    terms = query_terms(query)
    words = query_terms(source["title"] + " " + text)
    relevance = 4 * len(terms & words) / max(1, len(terms))
    # Prices, dates and measurements make useful detail pages easier to find.
    # This is ranking only, not a claim that a price is current or regional.
    details = len(re.findall(r"(?:[$\u00a3\u20ac]\s*\d[\d,.]*|\d[\d,.]*\s*(?:USD|EUR|GBP|%|kWh|GB)\b)", text))
    return ((5 if source["kind"] == "page" else 0) + relevance
            + min(4, details * .6) + min(1.5, len(text) / 1200))


def select_sources(found, query):
    """Greedy relevance with soft host diversity and near-duplicate removal."""
    candidates = [source for source in found if source]
    selected, hosts, fingerprints, seen = [], {}, [], set()
    while candidates and len(selected) < MAX_SOURCES:
        def score(source):
            host = urlsplit(source["url"]).hostname.removeprefix("www.")
            return _source_score(source, query) - 4 * hosts.get(host, 0)
        source = max(candidates, key=score)
        candidates.remove(source)
        fingerprint = " ".join(re.findall(r"\w+", source["text"].casefold()))
        if source["url"] in seen or any(
                fingerprint == prior or (len(fingerprint) > 120 and len(prior) > 120
                    and SequenceMatcher(None, fingerprint, prior).ratio() >= .9)
                for prior in fingerprints):
            continue
        selected.append(source)
        seen.add(source["url"])
        fingerprints.append(fingerprint)
        host = urlsplit(source["url"]).hostname.removeprefix("www.")
        hosts[host] = hosts.get(host, 0) + 1
    return selected


def child_results(sources, query, seen):
    """At most one navigation hop, to relevant pages on the source's own host."""
    terms = query_terms(query)
    shopping = bool(terms & SHOPPING_TERMS)
    ranked = []
    blocked = {"login", "signin", "signup", "register", "account", "cart", "checkout",
               "logout", "privacy", "cookie", "cookies", "terms", "contact", "support"}
    for source in sources:
        if not source or source["kind"] != "page":
            continue
        parent = urlsplit(source["url"])
        for link in source.get("_links", ()):
            try:
                url = validate_public_url(urljoin(source["url"], link["href"]))
            except (UnsafeDestination, ValueError):
                continue
            parsed = urlsplit(url)
            if (parsed.hostname != parent.hostname or url in seen
                    or parsed.path == parent.path
                    or re.search(r"\.(?:jpg|jpeg|png|gif|svg|webp|css|js|zip|pdf|mp4|mp3)$", parsed.path, re.I)):
                continue
            label_terms = query_terms(link["text"])
            path_terms = query_terms(parsed.path.replace("-", " ").replace("_", " "))
            if blocked & (label_terms | path_terms):
                continue
            relevance = 2 * len(terms & label_terms) + len(terms & path_terms)
            context = link["text"] + " " + link.get("context", "")
            if shopping and len(PRICE.findall(context)) >= 2 and re.search(r"\d{1,3}%|sale|expires|ends", context, re.I):
                relevance += 8
            if relevance:
                ranked.append((relevance, {"url": url, "title": link["text"], "content": ""}))
    children = []
    for _, result in sorted(ranked, key=lambda item: item[0], reverse=True):
        if result["url"] not in seen:
            children.append(result)
            seen.add(result["url"])
        if len(children) == MAX_CHILD_PAGES:
            break
    return children


class WebSearchService:
    def __init__(self, *, base_url=None):
        self.base_url = validate_endpoint(base_url if base_url is not None else os.getenv("WEB_SEARCH_URL", ""))
        self.session = None
        self._session_lock = asyncio.Lock()
        self._search_slots = asyncio.Semaphore(2)
        self._page_slots = asyncio.Semaphore(4)

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
                return body["results"][:MAX_RESULTS]
        except WebSearchError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise WebSearchError("Web search did not return usable results. Try again shortly.") from exc

    async def _fetch_page(self, url):
        document = await self._fetch_document(url)
        return document["text"], document["url"]

    async def _fetch_document(self, url):
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
                    text, links = extract_document(decoded) if mime != "text/plain" else (decoded.strip(), [])
                    if not text:
                        raise PageUnavailable("A source contains no readable text.")
                    return {"text": text, "url": current, "links": links}
        raise PageUnavailable("A source redirected too many times.")

    async def _source(self, result, query=""):
        async with self._page_slots:
            return await self._source_unlocked(result, query)

    async def _source_unlocked(self, result, query=""):
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
            document = await asyncio.wait_for(self._fetch_document(url), timeout=7)
            text, final_url, links = document["text"], document["url"], document["links"]
            kind = "page"
        except UnsafeDestination:
            return None
        except (aiohttp.ClientError, asyncio.TimeoutError, PageUnavailable, UnicodeError, ValueError):
            text, final_url, kind = snippet, url, "snippet"
            links = []
        if not text.strip():
            return None
        return {"title": title, "url": final_url, "text": select_excerpt(text, query),
                "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind,
                "_links": links}

    async def search(self, query):
        if (not isinstance(query, str) or not query.strip() or len(query.encode("utf-8")) > MAX_QUERY_BYTES
                or any(ord(char) < 32 and char not in "\n\t" for char in query)):
            raise WebSearchError("Give web search a question of at most 800 UTF-8 bytes.")
        try:
            return await asyncio.wait_for(self._search(query.strip()), timeout=SEARCH_DEADLINE)
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
            seen.update(source["url"] for source in found if source)
            children = child_results(found, query, seen)
            found.extend(await asyncio.gather(*(self._source(result, query) for result in children)))
            sources = [{"id": "S" + str(index), **{key: value for key, value in source.items()
                        if not key.startswith("_")}}
                       for index, source in enumerate(select_sources(found, query), 1)]
            if not sources:
                raise WebSearchError("No usable public sources came back. Try a more specific search.")
            return sources
