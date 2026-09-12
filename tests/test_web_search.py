import asyncio
import json
import socket
import unittest
from unittest.mock import AsyncMock, patch

from mediabot.services.web_search import (
    MAX_PAGE_BYTES, MAX_TEXT_BYTES, PageUnavailable, UnsafeDestination,
    WebSearchError, WebSearchService, _PinnedResolver, _read_bounded,
    _resolve_public, extract_text, select_excerpt, validate_public_url,
)

URL = "http://web-search:8080"
PUBLIC = [(socket.AF_INET, "93.184.215.14")]


class Content:
    def __init__(self, raw):
        self.raw = raw

    async def iter_chunked(self, size):
        for index in range(0, len(self.raw), size):
            yield self.raw[index:index + size]


class Response:
    def __init__(self, status=200, body=None, headers=None, raw=None):
        self.status = status
        self.headers = headers or {"Content-Type": "text/html"}
        self.charset = "utf-8"
        self.content = Content(raw if raw is not None else json.dumps(body).encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    closed = False

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.close = AsyncMock()

    def post(self, url, **options):
        self.calls.append((url, options))
        return next(self.responses)

    get = post

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class WebSearchTests(unittest.IsolatedAsyncioTestCase):
    def service(self, body=None, status=200, raw=None):
        service = WebSearchService(base_url=URL)
        service.session = Session(Response(status, body, raw=raw))
        return service

    async def test_configured_search_endpoint_is_fixed_and_optional(self):
        self.assertFalse(WebSearchService(base_url="").enabled)
        self.assertTrue(WebSearchService(base_url="http://127.0.0.1:11889/").enabled)
        for value in ("https://example.com", "http://10.0.0.55:8123", URL + "/admin",
                      URL + "?x=1", URL + "#fragment", "http://me:secret@web-search:8080",
                      " " + URL, URL + "\n", "http://web-search:bad"):
            with self.subTest(value=value), self.assertRaises(WebSearchError):
                WebSearchService(base_url=value)
        with self.assertRaisesRegex(WebSearchError, "not configured"):
            await WebSearchService(base_url="").search("question")

    async def test_invalid_question_rejected_before_network(self):
        service = self.service()
        for value in (None, [], "", " ", "a" * 801, "\u00e9" * 401, "a\0b"):
            with self.subTest(value=value), self.assertRaises(WebSearchError):
                await service.search(value)
        self.assertEqual(service.session.calls, [])

    async def test_private_and_tricky_urls_rejected(self):
        for value in ("file:///etc/passwd", "http://127.0.0.1", "http://10.0.0.52",
                      "http://100.64.134.56", "http://169.254.169.254/latest/meta-data",
                      "http://[::1]", "http://[::ffff:127.0.0.1]", "http://[64:ff9b::a00:1]",
                      "http://[2002:7f00:1::]", "http://[ff02::1]", "http://224.0.0.1",
                      "https://site.local", "http://localhost.", "http://homeassistant",
                      "http://example.com:8123", "https://user:secret@example.com/",
                      "http://example.com\\@10.0.0.1/", "https://example.com/\n",
                      "http://127%2e0%2e0%2e1/", "http://[fe80::1%25eth0]/"):
            with self.subTest(value=value), self.assertRaises(UnsafeDestination):
                validate_public_url(value)
        self.assertEqual(validate_public_url("https://EXAMPLE.com:443/path#part"), "https://example.com/path")

    async def test_dns_rejects_any_private_answer_and_numeric_host_disguises(self):
        loop = asyncio.get_running_loop()
        def rows(*addresses):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443)) for address in addresses]
        for addresses in (("127.0.0.1",), ("93.184.215.14", "10.0.0.1"), ("100.64.134.56",), ()):
            with patch.object(loop, "getaddrinfo", AsyncMock(return_value=rows(*addresses))):
                with self.assertRaises(UnsafeDestination):
                    await _resolve_public("2130706433", 443)
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=rows("93.184.215.14"))):
            self.assertEqual(await _resolve_public("example.com", 443), PUBLIC)

    async def test_pinned_resolver_never_resolves_dns_again_or_changes_hostname(self):
        resolver = _PinnedResolver("example.com", PUBLIC)
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=AssertionError("second DNS lookup"))):
            answers = await resolver.resolve("example.com", 443)
        self.assertEqual(answers[0]["host"], PUBLIC[0][1])
        self.assertEqual(answers[0]["hostname"], "example.com")
        with self.assertRaises(UnsafeDestination):
            await resolver.resolve("changed.example", 443)
        with self.assertRaises(UnsafeDestination):
            await _PinnedResolver("example.com", [(socket.AF_INET, "10.0.0.1")]).resolve("example.com", 443)

    async def test_redirect_private_destination_never_requested(self):
        service = self.service()
        session = Session(Response(302, headers={"Location": "http://169.254.169.254/"}))
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch("mediabot.services.web_search.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(UnsafeDestination):
                await service._fetch_page("https://example.com")
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][1]["allow_redirects"])

    async def test_redirect_dns_is_revalidated_even_same_hostname(self):
        service = self.service()
        session = Session(Response(302, headers={"Location": "/changed"}))
        resolver = AsyncMock(side_effect=[PUBLIC, UnsafeDestination("rebound")])
        with patch("mediabot.services.web_search._resolve_public", resolver), \
                patch("mediabot.services.web_search.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(UnsafeDestination):
                await service._fetch_page("https://example.com")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(resolver.await_count, 2)

    async def test_redirect_limit_and_plain_text_extraction(self):
        service = self.service()
        session = Session(*(Response(302, headers={"Location": "/next"}) for _ in range(4)))
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch("mediabot.services.web_search.aiohttp.ClientSession", return_value=session):
            with self.assertRaisesRegex(PageUnavailable, "too many"):
                await service._fetch_page("https://example.com")
        self.assertEqual(len(session.calls), 4)
        session = Session(Response(raw=b"<head>hidden</head><article><p>Real answer</p><script>evil()</script></article>"))
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch("mediabot.services.web_search.aiohttp.ClientSession", return_value=session) as factory:
            text, final_url = await service._fetch_page("https://example.com")
        self.assertEqual((text, final_url), ("Real answer", "https://example.com/"))
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertFalse(factory.call_args.kwargs["auto_decompress"])

    async def test_stream_limits_include_missing_and_misleading_content_length(self):
        for headers in ({}, {"Content-Length": "1"}, {"Content-Length": str(MAX_PAGE_BYTES + 1)}):
            with self.subTest(headers=headers), self.assertRaises(PageUnavailable):
                await _read_bounded(Response(raw=b"x" * (MAX_PAGE_BYTES + 1), headers=headers), MAX_PAGE_BYTES)
        with self.assertRaises(PageUnavailable):
            await _read_bounded(Response(raw=b"tiny", headers={"Content-Encoding": "gzip"}), MAX_PAGE_BYTES)

    async def test_only_returned_snippet_used_on_fetch_failure(self):
        service = self.service()
        result = {"url": "https://example.com", "title": "Example", "content": "Provider snippet."}
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch.object(service, "_fetch_page", AsyncMock(side_effect=PageUnavailable("blocked"))):
            source = await service._source(result)
        self.assertEqual((source["kind"], source["text"]), ("snippet", "Provider snippet."))
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch.object(service, "_fetch_page", AsyncMock(side_effect=UnsafeDestination("rebound"))):
            self.assertIsNone(await service._source(result))
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(side_effect=UnsafeDestination("private"))):
            self.assertIsNone(await service._source(result))

    async def test_search_posts_only_query_and_bounds_sources_deduplicating_urls(self):
        rows = [{"url": "https://example.com/" + str(i), "title": "Title", "content": "Snippet"} for i in range(8)]
        service = self.service({"results": rows})
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch.object(service, "_fetch_page", AsyncMock(side_effect=lambda url: ("\u00e9" * 3000, url))):
            sources = await service.search("  original question  ")
        self.assertEqual([s["id"] for s in sources], ["S1", "S2", "S3"])
        self.assertEqual(sum(len(s["text"].encode()) for s in sources), MAX_TEXT_BYTES)
        self.assertTrue(all(s["kind"] == "page" and s["retrieved_at"].endswith("+00:00") for s in sources))
        url, options = service.session.calls[0]
        self.assertEqual(url, URL + "/search")
        self.assertEqual(options["data"]["q"], "original question")
        self.assertNotIn("messages", options["data"])
        self.assertFalse(options["allow_redirects"])
        service = self.service({"results": [rows[0], rows[0], {**rows[0], "url": rows[0]["url"] + "#part"}]})
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch.object(service, "_fetch_page", AsyncMock(return_value=("Text", rows[0]["url"]))) as fetch:
            self.assertEqual(len(await service.search("question")), 1)
        fetch.assert_awaited_once()

    async def test_provider_failure_no_results_and_invalid_json_are_useful(self):
        for service, expected in ((self.service({}, status=403), "unavailable"),
                                  (self.service({"results": []}), "No usable"),
                                  (self.service({"results": [None, {"url": "http://10.0.0.1"}]}), "No usable"),
                                  (self.service(raw=b"not-json private debug contents"), "usable results")):
            with self.subTest(expected=expected), self.assertRaisesRegex(WebSearchError, expected) as caught:
                await service.search("question")
            self.assertNotIn("private debug", str(caught.exception))

    async def test_sessions_have_no_cookies_proxy_or_credentials_and_close(self):
        service = WebSearchService(base_url=URL)
        session = Session(Response())
        with patch("mediabot.services.web_search.aiohttp.ClientSession", return_value=session) as factory:
            await asyncio.gather(service.start(), service.start())
        factory.assert_called_once()
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertNotIn("Authorization", factory.call_args.kwargs["headers"])
        await service.close()
        session.close.assert_awaited_once()

    def test_html_is_text_only_with_script_nav_and_form_removed(self):
        html = "<nav>Menu</nav><main><h1>Price</h1><p>$80 &amp; 30% off</p><form>Sign in</form><style>hidden</style></main>"
        self.assertEqual(extract_text(html), "Price\n$80 & 30% off")

    def test_main_or_article_preferred_over_cookie_and_site_furniture(self):
        self.assertEqual(extract_text("<header>Brand</header><div>Cookies</div><main><article>Real story</article></main><aside>Ads</aside>"), "Real story")
        self.assertEqual(extract_text("<header>Brand</header><article>Real story</article><aside>Ads</aside>"), "Real story")
        self.assertEqual(extract_text("<nav><nav>nested menu</nav>outer menu</nav><p>Content</p>"), "Content")

    def test_query_passages_include_late_answer_with_source_words_and_budget(self):
        prefix = "Navigation and cookie preferences.\n" * 150
        answer = "Nintendo US store deals: Example Game costs $56 through September 20."
        suffix = "Other information.\n" * 150
        excerpt = select_excerpt(prefix + answer + "\n" + suffix, "any Nintendo US store deals?")
        self.assertIn(answer, excerpt)
        self.assertLessEqual(len(excerpt.encode()), 2000)
        self.assertNotIn("$30", excerpt)
        long = "\u00e9 " * 1500 + "Nintendo US store deals cost $56 today. " + "\u00e9 " * 1500
        self.assertIn("Nintendo US store deals", select_excerpt(long, "Nintendo US store deals"))
        self.assertLessEqual(len(select_excerpt(long, "Nintendo US store deals").encode()), 2000)

    async def test_empty_stripped_title_falls_back_to_public_hostname(self):
        service = self.service()
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch.object(service, "_fetch_page", AsyncMock(return_value=("Source text", "https://example.com/"))):
            source = await service._source({"url": "https://example.com", "title": "<script>invisible</script>"})
        self.assertEqual(source["title"], "example.com")


if __name__ == "__main__":
    unittest.main()
