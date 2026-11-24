import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.async_api import async_playwright
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from linuxdo.browser import Chrome
from linuxdo.capture import MAX_CAPTURE_BYTES, CapturedPage, Captures
from linuxdo.server import page_capture

ORIGIN = "chrome-extension://" + "a" * 32
HEADERS = {"Origin": ORIGIN, "Authorization": "Bearer fixture"}


class CaptureTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.captures = Captures(root / "captures.sqlite3")
        self.captures.initialize()
        app = Starlette(routes=[Route("/page-capture", page_capture, methods=["POST"])])
        app.state.capture_key = b"fixture"
        app.state.captures = self.captures
        self.client = self.enterContext(TestClient(app, base_url="http://127.0.0.1:8765"))

    def test_forum_pages_are_saved_and_partitioned(self):
        self.client.post("/page-capture", headers=HEADERS, json={
            "url": "https://linux.do/latest",
            "title": "最新主题",
            "links": [{"title": "采集样例", "url": "https://linux.do/t/topic/42?token=secret#reply"}],
        }).raise_for_status()
        url = "https://v2ex.com/t/100?p=2&token=secret#r_101"
        for text in ("初稿", "更新"):
            response = self.client.post("/page-capture", headers=HEADERS, json={
                "url": url, "title": "主题",
                "body": "正文", "posts": [{"number": 2, "text": text}],
            })
            self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.captures.list(None, 10)), 2)
        self.assertEqual(len(self.captures.list("v2ex", 10)), 1)
        self.assertEqual(self.captures.read(url)["posts"][0]["text"], "更新")
        self.assertEqual(self.captures.read(url)["url"], "https://www.v2ex.com/t/100?p=2")
        latest = self.captures.read("https://linux.do/latest")
        self.assertEqual(latest["topics"][0]["url"], "https://linux.do/t/topic/42")

    def test_rejects_invalid_sources_and_oversize(self):
        page = {"url": "https://linux.do/t/topic/42", "title": "主题", "body": "正文"}
        self.assertEqual(self.client.post("/page-capture", json=page).status_code, 403)
        self.assertEqual(self.client.post("/page-capture", json=page, headers={"Origin": ORIGIN}).status_code, 401)
        for url in ("https://linux.do@invalid.test/t/42", "https://linux.do/login", "https://www.v2ex.com/api/v2/"):
            self.assertEqual(self.client.post("/page-capture", headers=HEADERS, json={
                **page, "url": url,
            }).status_code, 422)
        extra = self.client.post("/page-capture", headers=HEADERS, json={**page, "cookie": "not allowed"})
        self.assertEqual(extra.status_code, 422)
        large = self.client.post("/page-capture", headers={**HEADERS, "Content-Type": "application/json"},
                                 content=b" " * (MAX_CAPTURE_BYTES + 1))
        self.assertEqual(large.status_code, 413)
        self.assertEqual(self.captures.list(None, 10), [])


class ExtensionExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_two_forums_and_stops_at_challenge(self):
        code = Path("extension/capture.js").read_text(encoding="utf-8")
        script = "(" + code.removeprefix("export ").strip() + ")"
        root = Path(self.enterContext(TemporaryDirectory()))
        pw = await self.enterAsyncContext(async_playwright())
        chrome = Chrome(pw, root)
        self.addAsyncCleanup(chrome.close)
        await chrome.start("capture-test")
        page = await chrome.context.new_page()
        fixtures = [
            ("https://linux.do/t/topic/42", '''
                <h1 data-topic-id="42"><a class="fancy-title">Linux 主题</a></h1>
                <article id="post_1" data-post-id="1"><div class="cooked">Linux 正文</div></article>
                <article id="post_2" data-post-id="2"><div class="cooked">Linux 回复</div></article>
            ''', "linuxdo", "topic", 2),
            ("https://www.v2ex.com/t/42?p=2", '''
                <main id="Main"><div class="header"><h1>V2EX 主题</h1></div>
                <div class="topic_content">V2EX 正文</div>
                <div id="r_101"><span class="no">2</span>
                <div class="reply_content">V2EX 回复</div></div></main>
            ''', "v2ex", "topic", 1),
            ("https://v2ex.com/recent", '''
                <div id="Main"><span class="item_title"><a href="https://www.v2ex.com/t/1#r_2">主题</a></span>
                <span class="item_title"><a href="https://invalid.test/t/2">站外</a></span></div>
            ''', "v2ex", "list", 1),
        ]
        for url, html, forum, kind, count in fixtures:
            async def fulfill(route):
                await route.fulfill(content_type="text/html; charset=utf-8", body=html)

            await page.route(url, fulfill, times=1)
            await page.goto(url)
            found = await page.evaluate(script)
            actual_forum, actual_kind, data = CapturedPage.model_validate(found["page"]).render()
            self.assertEqual((actual_forum, actual_kind), (forum, kind))
            self.assertEqual(len(data["posts"] if kind == "topic" else data["topics"]), count)

        async def challenge(route):
            await route.fulfill(status=403, content_type="text/html", body="<title>Just a moment...</title>")

        await page.route("https://linux.do/latest", challenge, times=1)
        await page.goto("https://linux.do/latest")
        self.assertEqual(await page.evaluate(script), {"state": "manual_verification"})
