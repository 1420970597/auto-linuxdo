import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

from linuxdo.cookies import parse_cookie_input
from linuxdo.state import Hold, Store


class CookieInputTests(unittest.TestCase):
    def test_header_only_imports_forum_cookies(self):
        cookies = parse_cookie_input("Cookie: cf_clearance=skip; _t=opaque%2Bvalue==; _forum_session=keep")
        self.assertEqual(
            [(cookie["name"], cookie["value"]) for cookie in cookies],
            [("_forum_session", "keep"), ("_t", "opaque%2Bvalue==")],
        )
        self.assertTrue(all(cookie["secure"] and cookie["httpOnly"] for cookie in cookies))
        self.assertTrue(all("expires" not in cookie for cookie in cookies))

    def test_json_only_imports_current_root_forum_cookies(self):
        expires = int(time()) + 100
        cookies = parse_cookie_input(json.dumps({"cookies": [
            {"name": "_t", "value": "other", "domain": "example.invalid"},
            {"name": "_t", "value": "other-url", "url": "https://example.invalid/"},
            {"name": "_t", "value": "subpath", "path": "/other"},
            {"name": "_t", "value": "old", "expires": 1},
            {"name": "_t", "value": "live", "domain": ".linux.do", "expirationDate": expires},
        ]}))
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["value"], "live")
        self.assertEqual(cookies[0]["expires"], expires)

    def test_rejects_missing_or_ambiguous_auth_cookie(self):
        for input_text in ("cf_clearance=challenge", "_t=first; _t=second", "_t=broken\nvalue"):
            with self.subTest(input_text=input_text), self.assertRaises(ValueError):
                parse_cookie_input(input_text)


class StoreMigrationTests(unittest.TestCase):
    def test_retired_chrome_state_and_its_hold_are_discarded(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        (root / "chrome" / "profile").mkdir(parents=True)
        (root / "chrome-generation").write_text("fixture")
        (root / "page-capture-token").write_text("fixture")
        (root / "captured-pages.sqlite3").write_text("fixture")
        store = Store(root)
        store.set_hold(Hold("manual_verification", "verification required"))
        store.discard_retired_state()
        self.assertFalse((root / "chrome").exists())
        self.assertFalse((root / "chrome-generation").exists())
        self.assertFalse((root / "page-capture-token").exists())
        self.assertFalse((root / "captured-pages.sqlite3").exists())
        self.assertIsNone(store.hold())

    def test_retired_file_cleanup_keeps_v2ex_human_verification_hold(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        (root / "chrome").mkdir()
        store = Store(root)
        store.set_hold(Hold("manual_verification", "v2ex verification required"))
        store.discard_retired_files()
        self.assertFalse((root / "chrome").exists())
        self.assertIsNotNone(store.hold())

    def test_retired_migration_keeps_site_wide_holds(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        store = Store(root)
        store.set_hold(Hold("site_blocked", "access denied"))
        store.discard_retired_state()
        hold = store.hold()
        assert hold is not None
        self.assertEqual(hold.code, "site_blocked")
