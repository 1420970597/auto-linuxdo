import json
import unittest
from time import time

from linuxdo.cookies import parse_cookie_input


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
