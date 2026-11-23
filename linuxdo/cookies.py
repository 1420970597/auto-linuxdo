import json
import re
from time import time
from urllib.parse import urlsplit

ORIGIN = "https://linux.do"
ALLOWED_COOKIES = {"_t", "_forum_session"}
COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,3800}")
MAX_IMPORT_BYTES = 16_384


def parse_cookie_input(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith(("[", "{")):
        data = json.loads(text)
        return normalize_cookies(data.get("cookies") if isinstance(data, dict) else data)

    if text[:7].lower() == "cookie:":
        text = text[7:]
    entries = []
    for part in text.split(";"):
        name, equals, value = part.strip().partition("=")
        if name in ALLOWED_COOKIES and equals:
            entries.append({"name": name, "value": value})
    return normalize_cookies(entries)


def normalize_cookies(entries: object) -> list[dict]:
    if not isinstance(entries, list):
        raise ValueError("请提供 Cookie 请求头、Cookie JSON 数组或包含 cookies 的 JSON 对象。")
    accepted = {}
    now = time()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Cookie 项必须是 JSON 对象。")
        name = entry.get("name")
        if not isinstance(name, str) or name not in ALLOWED_COOKIES:
            continue
        domain = entry.get("domain", "linux.do")
        if domain not in ("linux.do", ".linux.do") or entry.get("path", "/") != "/":
            continue
        if "url" in entry:
            if not isinstance(entry["url"], str):
                continue
            url = urlsplit(entry["url"])
            if (url.scheme, url.netloc) != ("https", "linux.do"):
                continue
        if entry.get("partitionKey") is not None:
            continue
        value = entry.get("value")
        if not isinstance(value, str) or not COOKIE_VALUE.fullmatch(value):
            raise ValueError("论坛 Cookie 的值无效。")
        expires = entry.get("expires", entry.get("expirationDate", -1))
        if type(expires) not in (int, float) or not -1 <= expires <= 253402300799:
            raise ValueError("Cookie 到期时间无效。")
        if expires != -1 and expires <= now:
            continue
        if name in accepted:
            raise ValueError("Cookie 中存在重复的论坛会话项。")
        client_cookie = {
            "name": name,
            "value": value,
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
        }
        if expires != -1:
            client_cookie["expires"] = expires
        accepted[name] = client_cookie
    if "_t" not in accepted:
        raise ValueError("缺少有效的 Linux.do _t 会话 Cookie。")
    return [accepted[name] for name in sorted(accepted)]
