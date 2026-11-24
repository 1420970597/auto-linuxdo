import json
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import cast
from urllib.parse import parse_qs, urlsplit

from linuxdo.state import ForumError, Store
from linuxdo.v2ex import V2EX, configure_token

TITLE = "V2EX 示例主题"
TOPIC = {"id": 42, "title": TITLE, "content": "主题正文。", "url": "https://example.invalid"}
REPLIES = [{"id": 123, "content": "示例回复。"}]
LIST_HTML = f'''
<div class="cell item"><span class="item_title"><a href="/t/42#reply1" class="topic-link">{TITLE}</a></span></div>
<div class="cell item"><span class="item_title"><a href="/t/43" class="topic-link">其他示例</a></span></div>
'''
TOPIC_HTML = f'''
<div class="box"><div class="header"><h1>{TITLE}</h1></div>
<div class="cell"><div class="topic_content">主题正文。</div></div></div>
<div id="topic-replies">
<div class="cell" id="r_123"><span class="no">1</span><div class="reply_content">示例回复。</div></div>
</div>
'''


class Server(ThreadingHTTPServer):
    requests: list
    block: tuple | None
    redirect_api: bool


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def respond(self, body="", status=200, content_type="text/html", **headers):
        content = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path
        server = cast(Server, self.server)
        server.requests.append(
            (self.path, self.headers.get("Authorization"), self.headers.get("Cookie"))
        )
        if server.block:
            status, headers = server.block
            self.respond("Blocked", status, **headers)
            return

        if path.startswith("/api/v2/"):
            self.handle_api(path)
            return

        if path in ("/recent", "/go/python"):
            body = LIST_HTML
            if parse_qs(url.query).get("p") == ["1"]:
                body += f'<link rel="next" href="{path}?p=2">'
        elif path == "/t/42":
            body = TOPIC_HTML + f'<a href="{path}?p=2" class="page_normal">2</a>'
        elif path == "/t/43":
            self.respond(status=302, Location="/signin")
            return
        elif path == "/signin":
            body = '<form><input type="password"></form>'
        elif path == "/wrong":
            body = ""
        else:
            self.respond(status=404)
            return

        self.respond(f'<!doctype html><div id="Main">{body}</div>')

    def handle_api(self, path):
        if cast(Server, self.server).redirect_api:
            self.respond(status=302, Location="/wrong")
            return
        if self.headers.get("Authorization") != "Bearer fixture-only":
            self.respond('{"success":false,"message":"Token expired"}', 401, "application/json")
            return
        result = {
            "/api/v2/nodes/python/topics": [TOPIC],
            "/api/v2/topics/42": TOPIC,
            "/api/v2/topics/42/replies": REPLIES,
        }.get(path)
        if result is None:
            self.respond(status=404)
        else:
            self.respond(
                json.dumps({"success": True, "result": result}), content_type="application/json"
            )


class V2EXTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.http = Server(("127.0.0.1", 0), Fixture)
        self.http.requests = []
        self.http.block = None
        self.http.redirect_api = False
        thread = Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.origin = f"http://127.0.0.1:{self.http.server_port}"
        self.store = Store(root)
        self.forum = V2EX(self.store, origin=self.origin)

    async def test_anonymous_recent_node_topic_and_replies(self):
        recent = await self.forum.latest(1, 1)
        self.assertEqual(
            recent["topics"], [{"topic_id": 42, "title": TITLE, "url": self.origin + "/t/42"}]
        )
        self.assertEqual(recent["next_page"], 2)
        self.assertTrue(recent["truncated"])

        node = await self.forum.node_topics("python", 2, 10)
        self.assertEqual(node["page"], 2)
        self.assertIsNone(node["next_page"])
        self.assertEqual((await self.forum.read_topic(42))["text"], "主题正文。")
        reply = (await self.forum.replies(42, 1, 5))["replies"][0]
        self.assertEqual(reply["url"], self.origin + "/t/42?p=1#r_123")
        self.assertEqual(reply["text"], "示例回复。")
        self.assertEqual(self.forum.status()["state"], "ready")
        with self.assertRaises(ForumError) as caught:
            await self.forum.read_topic(43)
        self.assertEqual(caught.exception.code, "v2ex_login_required")
        self.assertTrue(all(auth is None for _, auth, _ in self.http.requests))

    async def test_official_api_sends_token_only_to_expected_requests(self):
        configure_token(self.forum.token_path, "fixture-only")
        self.assertEqual(self.forum.token_path.stat().st_mode & 0o777, 0o600)

        self.assertEqual((await self.forum.node_topics("python", 1, 10))["topics"][0]["topic_id"], 42)
        topic = await self.forum.read_topic(42)
        self.assertEqual(topic["url"], self.origin + "/t/42")
        self.assertEqual(topic["text"], "主题正文。")
        self.assertEqual((await self.forum.replies(42, 2, 5))["replies"][0]["text"], "示例回复。")
        await self.forum.latest(1, 1)
        requests = self.http.requests
        api_requests = [(path, auth) for path, auth, _ in requests if path.startswith("/api/")]
        self.assertEqual(len(api_requests), 3)
        self.assertTrue(all(auth == "Bearer fixture-only" for _, auth in api_requests))
        self.assertTrue(
            all(auth is None for path, auth, _ in requests if not path.startswith("/api/"))
        )

        self.http.redirect_api = True
        with self.assertRaises(ForumError) as caught:
            await self.forum.read_topic(42)
        self.assertEqual(caught.exception.code, "unexpected_page")

        self.http.redirect_api = False
        configure_token(self.forum.token_path, "expired")
        with self.assertRaises(ForumError) as caught:
            await self.forum.read_topic(42)
        self.assertEqual(caught.exception.code, "v2ex_token_rejected")
        self.assertNotIn("expired", str(caught.exception))

    async def test_verification_and_rate_limits_pause_reads(self):
        configure_token(self.forum.token_path, "fixture-only")
        for status, headers, code in (
            (403, {"cf-mitigated": "challenge"}, "manual_verification"),
            (403, {}, "site_blocked"),
            (429, {"Retry-After": "120"}, "rate_limited"),
        ):
            with self.subTest(code=code):
                self.http.block = status, headers
                with self.assertRaises(ForumError) as caught:
                    await self.forum.read_topic(42)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.forum.status()["state"], code)
                previous = len(self.http.requests)
                reopened = V2EX(self.store, origin=self.origin)
                with self.assertRaises(ForumError) as pause:
                    await reopened.latest(1, 10)
                self.assertEqual(pause.exception.code, code)
                self.assertEqual(len(self.http.requests), previous)
                if status == 429:
                    self.assertIn("retry_at", self.forum.status())
                self.store.clear_hold()
