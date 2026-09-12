import asyncio
import json
import socket
import unittest
from unittest.mock import AsyncMock, patch

from mediabot.services.web_search import (
    MAX_CHILD_PAGES, MAX_PAGE_BYTES, MAX_TEXT_BYTES, PageUnavailable, UnsafeDestination,
    WebSearchError, WebSearchService, _PinnedResolver, _read_bounded,
    _resolve_public, child_results, extract_document, extract_text, query_terms,
    select_excerpt, select_sources, validate_public_url,
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
                patch.object(service, "_fetch_document", AsyncMock(side_effect=PageUnavailable("blocked"))):
            source = await service._source(result)
        self.assertEqual((source["kind"], source["text"]), ("snippet", "Provider snippet."))
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch.object(service, "_fetch_document", AsyncMock(side_effect=UnsafeDestination("rebound"))):
            self.assertIsNone(await service._source(result))
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(side_effect=UnsafeDestination("private"))):
            self.assertIsNone(await service._source(result))

    async def test_search_posts_only_query_and_bounds_sources_deduplicating_urls(self):
        rows = [{"url": "https://example.com/" + str(i), "title": "Title", "content": "Snippet"} for i in range(8)]
        service = self.service({"results": rows})
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch.object(service, "_fetch_document", AsyncMock(side_effect=lambda url: {
                    "text": chr(256 + int(url.rsplit("/", 1)[1])) * 3000, "url": url, "links": []})):
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
                patch.object(service, "_fetch_document", AsyncMock(return_value={
                    "text": "Text", "url": rows[0]["url"], "links": []})) as fetch:
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
                patch.object(service, "_fetch_document", AsyncMock(return_value={
                    "text": "Source text", "url": "https://example.com/", "links": []})):
            source = await service._source({"url": "https://example.com", "title": "<script>invisible</script>"})
        self.assertEqual(source["title"], "example.com")

    def test_product_cards_keep_title_publisher_platform_price_and_region_together(self):
        intro = ("Best Nintendo Switch eShop deals\n"
                 "Prices come from the US and European stores; confirm your region.\n")
        cards = "\n".join("-90%\nGame Title " + str(i) + "\nPublisher " + str(i)
                          + "\nNintendo Switch\n$29.99 $2.99" for i in range(60))
        footer = "\nDiscounts are regional. Confirm the Nintendo US eShop price before buying."
        excerpt = select_excerpt(intro + cards + footer, "Any current Nintendo US eShop deals?")
        self.assertLessEqual(len(excerpt.encode()), 2000)
        self.assertLessEqual(excerpt.count("[...]"), 1)
        self.assertIn("US and European stores", excerpt)
        self.assertIn("Game Title 0\nPublisher 0\nNintendo Switch\n$29.99 $2.99", excerpt)
        # Repeated platform matches must not crowd out all product names.
        self.assertLessEqual(excerpt.count("Nintendo Switch") - excerpt.count("Game Title"), 2)

    async def test_large_realistic_html_keeps_late_answer_and_links(self):
        # Modern storefront bundles can exceed 256 KiB before the visible body.
        raw = ("<head><script>" + "var unused = 1;" * 50000 + "</script></head>"
               "<main><h1>Current camera deals</h1><article>"
               "Example Camera: USD 499, previously USD 699. Offer ends September 30."
               "<a href='/deals/cameras'><span>Camera</span> deals</a>"
               "</article></main>").encode()
        self.assertGreater(len(raw), 262144)
        service = self.service()
        session = Session(Response(raw=raw, headers={"Content-Type": "text/html", "Content-Length": str(len(raw))}))
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch("mediabot.services.web_search.aiohttp.ClientSession", return_value=session):
            document = await service._fetch_document("https://shop.example/")
        self.assertIn("Example Camera: USD 499, previously USD 699", document["text"])
        self.assertNotIn("var unused", document["text"])
        self.assertEqual(document["links"], [{"href": "/deals/cameras", "text": "Camera deals", "context": ""}])

    def test_ranking_retains_detail_page_over_duplicate_portals_and_snippets(self):
        def source(url, text, kind="page"):
            return {"url": url, "title": "Camera deals", "text": text, "kind": kind}
        portal = "See our camera deals and check the latest offers in your region. " * 5
        first = source("https://shop.example/", portal)
        duplicate = source("https://shop.example/offers", portal + " ")
        snippet = source("https://shop.example/catalog", "See camera deals and offers.", "snippet")
        detail = source("https://prices.example/camera", "Example Camera\nWas $699, now $499\nUS offer ends September 30.")
        independent = source("https://maker.example/camera", "Example Camera uses a 24 megapixel sensor.")
        selected = select_sources([first, duplicate, snippet, detail, independent], "Any current camera deals?")
        self.assertEqual(selected[0], detail)
        self.assertIn(independent, selected)
        self.assertEqual(sum(item["text"].strip() == portal.strip() for item in selected), 1)
        self.assertNotIn(snippet, selected)

    def test_child_links_keep_relevant_public_same_host_pages_only_and_bound_count(self):
        text, links = extract_document("""<nav><a href='/deals/nav'>Camera deals navigation</a></nav>
            <main><a href='/deals/cameras'>Camera deals</a>
            <a href='https://evil.example/deals'>Camera deals</a>
            <a href='http://127.0.0.1/deals'>Camera deals</a>
            <a href='/login?next=/deals'>Camera deals login</a>
            <a href='/deals/image.jpg'>Camera deals image</a>
            <a href='#fragment'>Camera deals</a>
            <a href='/deals/lenses'>Lens deals</a>
            <a href='/deals/cameras#another'>Camera deals</a>
            <a href='/deals/accessories'>Accessory deals</a></main>""")
        seed = {"url": "https://shop.example/", "text": text, "kind": "page", "_links": links}
        children = child_results([seed], "Any camera deals?", {seed["url"]})
        self.assertEqual(len(children), MAX_CHILD_PAGES)
        self.assertEqual([item["url"] for item in children], [
            "https://shop.example/deals/cameras", "https://shop.example/deals/lenses"])

    async def test_search_drills_down_once_and_uses_child_answer(self):
        service = self.service({"results": [{"url": "https://shop.example/", "title": "Camera deals"}]})
        async def document(url):
            if url == "https://shop.example/":
                return {"url": url, "text": "Browse the latest camera deals.", "links": [
                    {"href": "/deals/cameras", "text": "Camera deals"},
                    {"href": "/deals/lenses", "text": "Lens deals"},
                    {"href": "/deals/accessories", "text": "Accessory deals"}]}
            return {"url": url, "text": "Example Camera\nWas $699, now $499\nUS offer ends September 30.",
                    "links": [{"href": "/deals/recursive", "text": "More camera deals"}]}
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch.object(service, "_fetch_document", AsyncMock(side_effect=document)) as fetch:
            sources = await service.search("Any camera deals?")
        self.assertEqual(fetch.await_count, 1 + MAX_CHILD_PAGES)
        self.assertNotIn("https://shop.example/deals/recursive", [call.args[0] for call in fetch.await_args_list])
        self.assertIn("now $499", sources[0]["text"])
        self.assertEqual(sources[0]["url"], "https://shop.example/deals/cameras")
        self.assertTrue(all(set(source) == {"id", "url", "title", "text", "kind", "retrieved_at"} for source in sources))

    async def test_child_dns_rebinding_is_rejected_before_child_fetch(self):
        service = self.service({"results": [{"url": "https://shop.example/", "title": "Camera deals"}]})
        document = {"url": "https://shop.example/", "text": "Browse camera deals.", "links": [
            {"href": "/deals/cameras", "text": "Camera deals"}]}
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(side_effect=[PUBLIC, UnsafeDestination("private")])) as resolver, \
                patch.object(service, "_fetch_document", AsyncMock(return_value=document)) as fetch:
            sources = await service.search("Camera deals")
        self.assertEqual(resolver.await_count, 2)
        fetch.assert_awaited_once()
        self.assertEqual([source["url"] for source in sources], ["https://shop.example/"])

    async def test_eight_results_have_four_page_slots_and_cancel_all_fetches(self):
        rows = [{"url": "https://example.com/" + str(i), "title": "Answer"} for i in range(12)]
        service = self.service({"results": rows})
        started = asyncio.Event()
        active = maximum = cancelled = 0
        async def fetch(url):
            nonlocal active, maximum, cancelled
            active += 1
            maximum = max(maximum, active)
            if active == 4:
                started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled += 1
                raise
            finally:
                active -= 1
        with patch("mediabot.services.web_search._resolve_public", AsyncMock(return_value=PUBLIC)), \
                patch.object(service, "_fetch_document", AsyncMock(side_effect=fetch)) as calls:
            task = asyncio.create_task(service.search("question"))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual((maximum, active, cancelled, calls.await_count), (4, 0, 4, 4))

    def test_conversational_filler_does_not_outrank_subject_or_region(self):
        terms = query_terms("Could you check and see if there are camera deals on their site, if necessary drill down to US locale 80205?")
        self.assertEqual(terms, {"camera", "deals", "us", "80205"})

    def test_shopping_drilldown_prefers_product_with_adjacent_discount_over_navigation(self):
        text, links = extract_document("""<main><a href='/hottest'>Hottest camera deals</a>
            <article><a href='/items/camera-a'>Example Camera A</a>
            <div>$699</div><div>$499</div><div>-28%</div><div>Sale ends September 30</div></article>
            <article><a href='/items/camera-b'>Example Camera B</a>
            <div>$999</div><div>$799</div><div>-20%</div></article>
            <a href='/unrelated'>Unrelated page</a></main>""")
        seed = {"url": "https://shop.example/", "text": text, "kind": "page", "_links": links}
        children = child_results([seed], "Any camera deals?", {seed["url"]})
        self.assertEqual([item["url"] for item in children], [
            "https://shop.example/items/camera-a", "https://shop.example/items/camera-b"])
        self.assertNotIn("$699", links[0]["context"])
        self.assertNotIn("$999", links[1]["context"])
        self.assertEqual(child_results([seed], "What optics technology exists?", {seed["url"]}), [])

    def test_catalog_discount_first_excerpt_never_leaves_orphaned_title_or_price(self):
        intro = "Camera sale prices include US and European stores. Check the listed region.\n"
        cards = ["-90%\nExample Camera " + str(i) + "\nMaker " + str(i)
                 + "\nDigital camera\n$999.00 $99.90" for i in range(40)]
        excerpt = select_excerpt(intro + "\n".join(cards), "Any camera deals in US?", budget=700)
        self.assertTrue(excerpt.startswith(intro))
        self.assertEqual(excerpt.count("Example Camera"), excerpt.count("$999.00 $99.90"))
        self.assertEqual(excerpt.count("-90%"), excerpt.count("Example Camera"))
        self.assertEqual(excerpt.count("[...]"), 1)
        self.assertTrue(excerpt.endswith("$999.00 $99.90\n[...]"))
        self.assertLessEqual(len(excerpt.encode("utf-8")), 700)

    def test_catalog_discount_after_prices_keeps_expiry_with_complete_product(self):
        intro = "US digital shop offers, confirm retailer and region.\n"
        cards = ["Example Camera " + str(i) + "\n$699.00\n$499.00\n-28%\nLowest recorded price\nSale ends September 30"
                 for i in range(30)]
        excerpt = select_excerpt(intro + "\n".join(cards), "Any camera deals?", budget=700)
        self.assertEqual(excerpt.count("Example Camera"), excerpt.count("$499.00"))
        self.assertEqual(excerpt.count("Example Camera"), excerpt.count("Sale ends September 30"))
        self.assertTrue(excerpt.endswith("Sale ends September 30\n[...]"))
        self.assertLessEqual(len(excerpt.encode("utf-8")), 700)


if __name__ == "__main__":
    unittest.main()
