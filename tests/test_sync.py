import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from linuxdo.server import cookie_sync
from linuxdo.state import Hold, Store

ORIGIN = "chrome-extension://" + "a" * 32


class CookieSyncTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.store = Store(root)
        app = Starlette(routes=[
            Route("/cookie-sync", cookie_sync, methods=["GET", "POST", "DELETE"]),
            Route("/remote-cookie-sync", cookie_sync, methods=["POST", "DELETE"]),
        ])
        app.state.session = SimpleNamespace(store=self.store, status=lambda: {"state": "pending"})
        app.state.sync_key = b"local-fixture"
        app.state.remote_sync_key = b"remote-fixture"
        self.client = self.enterContext(TestClient(app, base_url="http://127.0.0.1:8765"))
        self.cookies = [{"name": "_t", "value": "forum-fixture"}]

    def test_remote_sync_requires_extension_origin_and_separate_key(self):
        remote = "/remote-cookie-sync"
        auth = {"Authorization": "Bearer remote-fixture"}
        self.assertEqual(self.client.post(remote, json=self.cookies, headers=auth).status_code, 403)
        headers = {"Origin": ORIGIN, "Authorization": "Bearer local-fixture"}
        self.assertEqual(self.client.post(remote, json=self.cookies, headers=headers).status_code, 401)
        headers.update(auth)
        self.assertEqual(self.client.post("/cookie-sync", json=self.cookies, headers=headers).status_code, 401)
        self.assertIsNone(self.store.cookie_config())
        reply = self.client.post(remote, json=self.cookies, headers=headers)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json(), {"state": "pending"})
        self.assertEqual(reply.headers["cache-control"], "no-store")
        self.assertEqual(self.store.cookie_config().source, "remote-browser")

    def test_remote_logout_does_not_delete_a_manually_imported_session(self):
        self.store.configure(self.cookies)
        remote = "/remote-cookie-sync"
        headers = {"Origin": ORIGIN, "Authorization": "Bearer remote-fixture"}
        self.assertEqual(self.client.delete(remote, headers=headers).status_code, 200)
        self.assertEqual(self.store.cookie_config().source, "manual")
        self.client.post(remote, json=self.cookies, headers=headers).raise_for_status()
        self.client.delete(remote, headers=headers).raise_for_status()
        self.assertIsNone(self.store.cookie_config())

    def test_remote_sync_preserves_site_access_pauses(self):
        headers = {"Origin": ORIGIN, "Authorization": "Bearer remote-fixture"}
        for hold in (
            Hold("manual_verification", "verification required"),
            Hold("site_blocked", "access denied"),
            Hold("rate_limited", "retry later", time() + 120),
        ):
            with self.subTest(hold=hold.code):
                self.store.set_hold(hold)
                for value in (hold.code, hold.code, hold.code + "-updated"):
                    response = self.client.post(
                        "/remote-cookie-sync",
                        json=[{"name": "_t", "value": value}],
                        headers=headers,
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(self.store.cookie_config().cookies[0]["value"], value)
                    self.assertEqual(self.store.hold(), hold)
