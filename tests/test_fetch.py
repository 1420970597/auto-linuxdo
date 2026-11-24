import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from linuxdo.fetch import Fetch
from linuxdo.state import ForumError


class FetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.profile = root / "profile.json"
        self.profile.write_text("{}")
        self.binary = root / "forum-fetch"
        self.store = root / "state"
        self.store.mkdir()

    def transport(self, body: str) -> Fetch:
        self.binary.write_text("#!/bin/sh\n" + body)
        self.binary.chmod(0o755)
        return Fetch(self.store, binary=str(self.binary), profile=str(self.profile))

    async def test_result_and_cookie_jar_are_taken_from_the_binary(self):
        fetch = self.transport(
            'printf %s \'{"url":"https://linux.do/latest.json","status":200,'
            '"html":"{}","cookies":{"cf_clearance":"fresh"},"solved":true}\'\n'
        )
        response = await fetch.get("https://linux.do/latest.json", cookies={"_t": "session"})
        self.assertEqual(response.status, 200)
        self.assertTrue(response.solved)
        jar = json.loads((self.store / "cf-cookies.json").read_text())
        self.assertEqual(jar, {"cf_clearance": "fresh"})

    async def test_error_payload_maps_to_a_site_hold(self):
        fetch = self.transport('printf %s \'{"error":"Cloudflare challenge could not be cleared"}\'\n')
        with self.assertRaises(ForumError) as caught:
            await fetch.get("https://linux.do/latest.json")
        self.assertEqual(caught.exception.code, "site_blocked")

    async def test_unparsable_output_reports_the_last_stderr_line(self):
        fetch = self.transport("echo 'connect failed: reset' >&2\nexit 1\n")
        with self.assertRaises(ForumError) as caught:
            await fetch.get("https://linux.do/latest.json")
        self.assertEqual(caught.exception.code, "site_unavailable")
        self.assertIn("connect failed: reset", str(caught.exception))

    async def test_missing_binary_is_reported_before_spawning(self):
        fetch = Fetch(self.store, binary=str(self.binary), profile=str(self.profile))
        with self.assertRaises(ForumError) as caught:
            await fetch.get("https://linux.do/latest.json")
        self.assertEqual(caught.exception.code, "transport_unavailable")
        self.assertFalse(os.path.exists(self.store / "cf-cookies.json"))
