import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import time
from urllib.parse import urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from linuxdo.html import text as html_text
from linuxdo.session import GLOBAL_HOLDS, iso_timestamp
from linuxdo.state import ForumError, Hold, Store, write_private

ORIGIN = "https://www.v2ex.com"
TOKEN_FILENAME = "personal-access-token"
TOPIC_PATH = re.compile(r"/t/([1-9][0-9]{0,9})")
TEXT_LIMIT = 12_000
TIMEOUT = 20.0
RATE_LIMIT_SECONDS = 900
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)
LOG = logging.getLogger(__name__)


def configure_token(path: Path, token: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", token):
        raise ValueError("V2EX 个人访问令牌格式无效。")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    write_private(path, token)


def as_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def clip_text(value: object, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def reply_url(origin: str, topic_id: int, page: int, reply_id: int | None) -> str:
    url = f"{origin}/t/{topic_id}?p={page}"
    return f"{url}#r_{reply_id}" if reply_id else url


def topic_links(html: str, limit: int, origin: str) -> list[dict]:
    tree = HTMLParser(html)
    topics = {}
    for node in tree.css("a.topic-link[href]"):
        href = node.attributes.get("href") or ""
        title = node.text(strip=True)
        path = urlsplit(href).path
        match = TOPIC_PATH.fullmatch(path)
        if not (match and title):
            continue
        topic_id = as_int(match[1])
        if topic_id is None:
            continue
        topics.setdefault(topic_id, {
            "topic_id": topic_id,
            "title": title[:300],
            "url": f"{origin}{path}",
        })
        if len(topics) == limit:
            break
    return list(topics.values())


def next_page(html: str, path: str, page: int) -> int | None:
    """The next page number, from the head link or the pager at the foot of the page."""
    tree = HTMLParser(html)
    for node in tree.css('link[rel="next"][href], a.page_normal[href], a[rel="next"][href]'):
        url = urlsplit(node.attributes.get("href") or "")
        if url.path == path and f"p={page + 1}" in url.query.split("&"):
            return page + 1
    return None


def page_title(html: str) -> str:
    tree = HTMLParser(html)
    node = tree.css_first("div.header h1") or tree.css_first("h1")
    return node.text(strip=True) if node else ""


def page_body(html: str) -> str:
    tree = HTMLParser(html)
    node = tree.css_first(".topic_content")
    return html_text(node.html or "") if node else ""


def page_replies(html: str, limit: int) -> list[dict]:
    tree = HTMLParser(html)
    replies = []
    seen = set()
    for node in tree.css('[id^="r_"]'):
        content = node.css_first(".reply_content")
        if content is None:
            continue
        raw = (node.attributes.get("id") or "")[2:]
        reply_id = as_int(raw)
        if reply_id in seen:
            continue
        seen.add(reply_id)
        number_node = node.css_first(".no")
        number = None
        if number_node is not None:
            value = number_node.text(strip=True)
            number = as_int(value)
        replies.append({
            "reply_id": reply_id,
            "number": number,
            "text": html_text(content.html or "")[:TEXT_LIMIT],
        })
        if len(replies) == limit:
            break
    return replies


def unexpected_result() -> ForumError:
    return ForumError("page_unrecognized", "无法识别 V2EX 返回的数据。")


def api_topic(item: object, origin: str) -> dict:
    if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 1:
        raise unexpected_result()
    topic_id = item["id"]
    title = clip_text(item.get("title"), 300)
    if not title:
        raise unexpected_result()
    return {"topic_id": topic_id, "title": title, "url": f"{origin}/t/{topic_id}"}


def retry_at(response: httpx.Response) -> float:
    value = response.headers.get("retry-after", "")
    seconds = as_int(value) or RATE_LIMIT_SECONDS
    return time() + min(seconds, RATE_LIMIT_SECONDS)


def check_response(response: httpx.Response, what: str) -> None:
    if response.status_code == 429:
        raise ForumError("rate_limited", "V2EX 暂时限流。", retry_at(response))
    if response.status_code == 403:
        if response.headers.get("cf-mitigated") == "challenge":
            raise ForumError("manual_verification", "V2EX 要求人工验证，访问已暂停。")
        raise ForumError("site_blocked", "V2EX 返回 HTTP 403，访问已暂停。")
    if response.status_code == 404:
        raise ForumError("not_found", f"V2EX 没有找到{what}。")
    if response.status_code >= 400:
        raise ForumError("site_unavailable", f"V2EX{what}返回 HTTP {response.status_code}。")


class V2EX:
    def __init__(self, store: Store, origin: str = ORIGIN):
        self.store = store
        self.origin = origin
        self.token_path = store.root / TOKEN_FILENAME
        self.lock = asyncio.Lock()

    def token(self) -> str | None:
        try:
            return self.token_path.read_text(encoding="ascii")
        except FileNotFoundError:
            return None

    def status(self) -> dict:
        result = {
            "state": "ready",
            "personal_access_token_configured": self.token_path.exists(),
            "transport": "http",
        }
        hold = self.store.hold()
        if hold and hold.applies("v2ex"):
            result.update(state=hold.code, message=hold.message)
            if hold.retry_at:
                result["retry_at"] = iso_timestamp(hold.retry_at)
        return result

    async def run(self, read: Callable[[httpx.AsyncClient], Awaitable[dict]]) -> dict:
        if self.lock.locked():
            raise ForumError("busy", "V2EX 正在处理另一项请求，请稍后再试。")
        async with self.lock:
            hold = self.store.hold()
            if hold and hold.applies("v2ex"):
                raise hold.error()
            try:
                async with httpx.AsyncClient(
                    base_url=self.origin,
                    follow_redirects=True,
                    timeout=TIMEOUT,
                    headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
                ) as client:
                    return await read(client)
            except httpx.HTTPError:
                raise ForumError("site_unavailable", "无法连接 V2EX。") from None
            except ForumError as error:
                if error.code in GLOBAL_HOLDS:
                    self.store.set_hold(Hold(error.code, str(error), error.retry_at))
                raise

    async def latest(self, page: int, limit: int) -> dict:
        return await self.run(
            lambda client: page_topics(client, "/recent", page, limit, self.origin)
        )

    async def node_topics(self, node: str, page: int, limit: int) -> dict:
        token = self.token()

        async def read(client):
            if token is None:
                return await page_topics(client, f"/go/{node}", page, limit, self.origin)
            data = await api_result(client, f"/nodes/{node}/topics?p={page}", token, list)
            topics = [api_topic(item, self.origin) for item in data[:limit]]
            return {"topics": topics, "page": page, "truncated": len(data) > limit}

        return await self.run(read)

    async def read_topic(self, topic_id: int) -> dict:
        token = self.token()

        async def read(client):
            if token is not None:
                data = await api_result(client, f"/topics/{topic_id}", token, dict)
                result = api_topic(data, self.origin)
                if result["topic_id"] != topic_id:
                    raise ForumError("unexpected_topic", "V2EX 返回了其他主题。")
                result["text"] = clip_text(data.get("content"), TEXT_LIMIT)
                return result

            html = await open_page(client, f"/t/{topic_id}", f"/t/{topic_id}")
            title = page_title(html)
            if not title:
                raise ForumError("page_unrecognized", "未找到可识别的 V2EX 主题。")
            return {
                "topic_id": topic_id,
                "url": f"{self.origin}/t/{topic_id}",
                "title": title[:300],
                "text": page_body(html),
            }

        return await self.run(read)

    async def replies(self, topic_id: int, page: int, limit: int) -> dict:
        token = self.token()

        async def read(client):
            if token is not None:
                data = await api_result(client, f"/topics/{topic_id}/replies?p={page}", token, list)
                replies = []
                for item in data[:limit]:
                    if not isinstance(item, dict):
                        raise unexpected_result()
                    reply_id = item.get("id")
                    if type(reply_id) is not int or reply_id < 1:
                        reply_id = None
                    replies.append({
                        "reply_id": reply_id,
                        "text": clip_text(item.get("content"), TEXT_LIMIT),
                        "url": reply_url(self.origin, topic_id, page, reply_id),
                    })
                return {"replies": replies, "page": page, "truncated": len(data) > limit}

            path = f"/t/{topic_id}"
            html = await open_page(client, f"{path}?p={page}", path, page)
            replies = page_replies(html, limit + 1)
            for reply in replies:
                reply["url"] = reply_url(self.origin, topic_id, page, reply["reply_id"])
            return {
                "replies": replies[:limit],
                "page": page,
                "next_page": next_page(html, path, page),
                "truncated": len(replies) > limit,
            }

        return await self.run(read)


async def open_page(client: httpx.AsyncClient, request: str, expected_path: str, page: int = 1) -> str:
    response = await client.get(request)
    check_response(response, "页面")
    final = urlsplit(str(response.url))
    if final.path == "/signin":
        raise ForumError("v2ex_login_required", "此页面需要登录，可在本机配置官方 V2EX 个人访问令牌。")
    if final.path.rstrip("/") != expected_path or (page > 1 and f"p={page}" not in final.query.split("&")):
        raise ForumError("unexpected_page", "V2EX 返回了其他页面。")
    return response.text


async def page_topics(
    client: httpx.AsyncClient, path: str, page: int, limit: int, origin: str
) -> dict:
    html = await open_page(client, f"{path}?p={page}", path, page)
    topics = topic_links(html, limit + 1, origin)
    return {
        "topics": topics[:limit],
        "page": page,
        "next_page": next_page(html, path, page),
        "truncated": len(topics) > limit,
    }


async def api_result(client: httpx.AsyncClient, path: str, token: str, result_type: type):
    response = await client.get(
        "/api/v2" + path,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    if urlsplit(str(response.url)).path != "/api/v2" + path.split("?")[0]:
        raise ForumError("unexpected_page", "V2EX API 返回了其他页面。")
    if not response.headers.get("content-type", "").lower().startswith("application/json"):
        check_response(response, " API ")
        raise unexpected_result()
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        raise unexpected_result() from None
    if response.status_code == 401 or (isinstance(data, dict) and data.get("message") == "Token expired"):
        raise ForumError("v2ex_token_rejected", "V2EX 个人访问令牌失效，请重新配置。")
    if response.status_code == 403:
        raise ForumError("site_blocked", "V2EX API 返回 HTTP 403，访问已暂停。")
    if response.status_code == 404:
        raise ForumError("not_found", "V2EX 没有找到请求的内容。")
    if response.status_code >= 400:
        raise ForumError("site_unavailable", f"V2EX API 返回 HTTP {response.status_code}。")
    ok = data.get("success") if isinstance(data, dict) else None
    if ok is not True or not isinstance(data.get("result"), result_type):
        raise unexpected_result()
    return data["result"]
