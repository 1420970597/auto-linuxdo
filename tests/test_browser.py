import asyncio
import unittest
from contextlib import suppress
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright

from linuxdo import forum
from linuxdo.browser import Chrome
from linuxdo.session import Session
from linuxdo.state import ForumError, Store

ACCOUNT = (
    '<form method="post" action="/session">'
    '<input id="login-account-name" name="username">'
    '<input id="login-account-password" name="password" type="password">'
    '<button id="login-button">Sign in</button></form>'
)
SIGNED_OUT = '<header class="d-header"><button class="login-button">Sign in</button></header>'
SIGNED_IN = '<header class="d-header"><button id="current-user">Profile</button></header>'
LATEST = (
    '<table><tr class="topic-list-item"><td class="main-link">'
    '<a class="raw-topic-link" href="/t/example/42">测试主题</a></td></tr></table>'
)
TOPIC = (
    '<h1 data-topic-id="42"><a class="fancy-title">测试主题</a></h1>'
    '<article id="post_1" data-post-id="123" data-topic-id="42">'
    '<div class="cooked"><p>Chrome 中渲染的测试正文。</p></div></article>'
    '<article id="post_2" data-post-id="124" data-topic-id="42">'
    '<div class="cooked"><p>第二篇帖子。</p></div></article>'
)


class ForumFixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_page(self, body, status=200, **headers):
        content = ("<!doctype html><meta charset=utf-8>" + body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        self.server.visits += 1
        if self.server.block_status == 429:
            self.send_page("Rate limited", 429, **{"Retry-After": "120"})
            return
        if self.server.block_status == 403:
            self.send_page("Forbidden", 403)
            return
        if self.server.block_status == 503:
            self.send_page("Verification required", 503, **{"cf-mitigated": "challenge"})
            return

        url = urlsplit(self.path)
        if url.path == "/login":
            self.send_page(ACCOUNT)
            return

        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        signed_in = cookie.get("fixture_session")
        if not signed_in or signed_in.value != self.server.session_id:
            self.send_page(SIGNED_OUT)
        elif url.path == "/latest":
            self.send_page(SIGNED_IN + LATEST)
        elif url.path.startswith("/t/topic/42"):
            self.send_page(SIGNED_IN + TOPIC)
        elif url.path == "/search":
            query = parse_qs(url.query).get("q")
            if query == ["docker & 沙箱"]:
                content = '<div class="fps-result"><a class="search-link" href="/t/example/42/2">命中主题</a></div>'
            else:
                content = '<div class="no-results-container">No results</div>'
            self.send_page(SIGNED_IN + content)
        else:
            self.send_page("Not found", 404)

    def do_POST(self):
        self.server.attempts += 1
        fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        if fields != {"username": ["demo"], "password": ["fixture-only"]}:
            self.send_page('<div class="login-fullpage"><div class="alert-error">Wrong password</div></div>')
        elif self.server.second_factor:
            self.send_page('<input id="login-second-factor">')
        else:
            cookie = f"fixture_session={self.server.session_id}; Path=/; HttpOnly; Max-Age=3600"
            self.send_page(SIGNED_IN, **{"Set-Cookie": cookie})


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.store = Store(Path(temporary))
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), ForumFixture)
        self.http.attempts = self.http.visits = 0
        self.http.block_status = None
        self.http.second_factor = False
        self.http.session_id = "first"
        thread = Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        playwright = await self.enterAsyncContext(async_playwright())
        origin = f"http://127.0.0.1:{self.http.server_port}"
        self.chrome = Chrome(playwright, self.store.root, origin=origin, spacing=0)
        self.addAsyncCleanup(self.chrome.close)
        self.session = Session(self.chrome, self.store, 3600)

    async def test_background_login_and_renewal(self):
        async def wait_for_login(attempts):
            async with asyncio.timeout(15):
                while (
                    self.http.attempts < attempts
                    or self.session.status()["state"] != "authenticated"
                    or self.session.lock.locked()
                ):
                    await asyncio.sleep(0.05)

        self.store.configure("demo", "fixture-only")
        background = asyncio.create_task(self.session.maintain())
        try:
            await wait_for_login(1)
            self.http.session_id = "expired"
            self.session.next_check = 0
            await wait_for_login(2)
            self.assertEqual(self.http.attempts, 2)
        finally:
            background.cancel()
            with suppress(asyncio.CancelledError):
                await background

    async def test_initial_login_profile_reuse_expiry_and_read(self):
        self.assertEqual(self.session.status()["state"], "credentials_required")
        self.store.configure("demo", "fixture-only")
        result = await self.session.run("/latest", lambda nav: forum.latest(nav, 10))
        self.assertEqual(result["topics"][0]["topic_id"], 42)
        self.assertEqual(self.session.status()["state"], "authenticated")
        self.assertTrue(self.chrome.sandbox_verified)
        self.assertEqual(self.http.attempts, 1)

        await self.chrome.close()
        self.session = Session(self.chrome, self.store, 3600)
        query = "docker & 沙箱"
        result = await self.session.run(forum.search_path(query), lambda nav: forum.search(nav, query, 5))
        self.assertEqual(result["topics"][0]["title"], "命中主题")
        self.assertEqual(self.http.attempts, 1)

        self.http.session_id = "updated"
        result = await self.session.run("/t/topic/42/2", lambda nav: forum.read_topic(nav, 42, 5, 2))
        self.assertEqual([post["post_number"] for post in result["posts"]], [2])
        self.assertEqual(result["posts"][0]["text"], "第二篇帖子。")
        self.assertEqual(self.http.attempts, 2)

        result = await self.session.run("/search?q=missing", lambda nav: forum.search(nav, "missing", 5))
        self.assertEqual(result["topics"], [])

    async def test_wrong_password_halts_and_new_credentials_take_effect(self):
        self.store.configure("demo", "wrong")
        with self.assertRaisesRegex(ForumError, "未接受登录"):
            await self.session.run("/latest")
        self.assertEqual(self.session.status()["state"], "login_rejected")
        previous = self.http.visits
        with self.assertRaises(ForumError):
            await self.session.run("/latest")
        self.assertEqual(self.http.visits, previous)
        self.assertEqual(self.http.attempts, 1)

        self.store.configure("demo", "fixture-only")
        await self.session.run("/latest")
        self.assertEqual(self.http.attempts, 2)

    async def test_second_factor_stops_after_one_submission(self):
        self.http.second_factor = True
        self.store.configure("demo", "fixture-only")
        with self.assertRaises(ForumError) as result:
            await self.session.run("/latest")
        self.assertEqual(result.exception.code, "manual_verification")
        self.assertEqual(self.session.status()["state"], "manual_verification")
        self.assertEqual(self.http.attempts, 1)
        with self.assertRaises(ForumError):
            await self.session.run("/latest")
        self.assertEqual(self.http.attempts, 1)

    async def test_access_controls_hold_across_service_restart(self):
        self.store.configure("demo", "fixture-only")
        for status, error in ((429, "rate_limited"), (403, "site_blocked"), (503, "manual_verification")):
            with self.subTest(status=status):
                self.http.block_status = status
                with self.assertRaises(ForumError) as result:
                    await self.session.run("/latest")
                self.assertEqual(result.exception.code, error)
                if status == 429:
                    self.assertIsNotNone(result.exception.retry_at)
                self.assertEqual(self.http.attempts, 0)
                previous = self.http.visits
                await self.chrome.close()
                self.session = Session(self.chrome, self.store, 3600)
                with self.assertRaises(ForumError) as retry:
                    await self.session.run("/latest")
                self.assertEqual(retry.exception.code, error)
                self.assertEqual(self.http.visits, previous)
                self.store.clear_hold()
