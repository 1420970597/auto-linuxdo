import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

from linuxdo.browser import Chrome, Navigator, same_origin
from linuxdo.session import GLOBAL_HOLDS, iso_timestamp
from linuxdo.state import ForumError, Hold, Store, write_private

ORIGIN = "https://www.v2ex.com"
TOKEN_FILE = "personal-access-token"
TOPIC_LINKS = "#Main .item_title a[href]"
TOPIC_TITLE = "#Main .header h1, #Main h1"
TOPIC_PATH = re.compile(r"/t/([1-9][0-9]{0,9})")
REPLY_CONTENT = '#Main [id^="r_"] .reply_content'
API_DOCUMENT = "/__auto_linuxdo_api_context__"
LOG = logging.getLogger(__name__)


def configure_token(path: Path, token: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", token):
        raise ValueError("V2EX 个人访问令牌格式无效。")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    write_private(path, token)


def clip_text(value: object, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def reply_url(origin: str, topic_id: int, page: int, reply_id: int | None) -> str:
    url = f"{origin}/t/{topic_id}?p={page}"
    return f"{url}#r_{reply_id}" if reply_id else url


class V2EX:
    def __init__(self, chrome: Chrome, store: Store):
        self.chrome = chrome
        self.store = store
        self.token_path = store.root / TOKEN_FILE
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
            "chrome_sandbox": self.chrome.sandbox_verified,
        }
        hold = self.store.hold()
        if hold and hold.applies("v2ex"):
            result.update(state=hold.code, message=hold.message)
            if hold.retry_at:
                result["retry_at"] = iso_timestamp(hold.retry_at)
        return result

    async def run(self, read: Callable[[Navigator], Awaitable[dict]]) -> dict:
        if self.lock.locked():
            raise ForumError("busy", "Chrome 正在处理另一项 V2EX 请求，请稍后再试。")
        async with self.lock:
            hold = self.store.hold()
            if hold and hold.applies("v2ex"):
                raise hold.error()
            try:
                await self.chrome.start("v2ex")
            except Exception as error:
                LOG.error("V2EX Chrome startup failed (%s)", type(error).__name__)
                raise ForumError("browser_unavailable", "V2EX 的 Chrome 沙箱未能启动。") from None

            try:
                async with self.chrome.page() as nav:
                    return await read(nav)
            except ForumError as error:
                if error.code in GLOBAL_HOLDS:
                    self.store.set_hold(Hold(error.code, str(error), error.retry_at))
                raise

    async def latest(self, page: int, limit: int) -> dict:
        return await self.run(lambda nav: page_topics(nav, "/recent", page, limit))

    async def node_topics(self, node: str, page: int, limit: int) -> dict:
        token = self.token()

        async def read(nav):
            if token is None:
                return await page_topics(nav, f"/go/{node}", page, limit)
            data = await api_result(nav, f"/nodes/{node}/topics?p={page}", token, list)
            topics = [api_topic(item, nav.origin) for item in data[:limit]]
            return {"topics": topics, "page": page, "truncated": len(data) > limit}

        return await self.run(read)

    async def read_topic(self, topic_id: int) -> dict:
        token = self.token()

        async def read(nav):
            if token is not None:
                data = await api_result(nav, f"/topics/{topic_id}", token, dict)
                result = api_topic(data, nav.origin)
                if result["topic_id"] != topic_id:
                    raise ForumError("unexpected_topic", "V2EX 返回了其他主题。")
                result["text"] = clip_text(data.get("content"), 12_000)
                return result

            path = f"/t/{topic_id}"
            await open_page(nav, path, path, 1)
            await nav.wait(TOPIC_TITLE)
            title = await nav.visible(TOPIC_TITLE).inner_text()
            body = await nav.visible("#Main .topic_content").evaluate_all(
                "(elements) => elements[0]?.innerText.trim().slice(0, 12000) ?? ''"
            )
            await nav.check()
            return {"topic_id": topic_id, "url": nav.origin + path, "title": title.strip()[:300], "text": body}

        return await self.run(read)

    async def replies(self, topic_id: int, page: int, limit: int) -> dict:
        token = self.token()

        async def read(nav):
            if token is not None:
                data = await api_result(nav, f"/topics/{topic_id}/replies?p={page}", token, list)
                replies = []
                for item in data[:limit]:
                    if not isinstance(item, dict):
                        raise unexpected_result()
                    reply_id = item.get("id")
                    if type(reply_id) is not int or reply_id < 1:
                        reply_id = None
                    replies.append({
                        "reply_id": reply_id,
                        "text": clip_text(item.get("content"), 12_000),
                        "url": reply_url(nav.origin, topic_id, page, reply_id),
                    })
                return {"replies": replies, "page": page, "truncated": len(data) > limit}

            path = f"/t/{topic_id}"
            await open_page(nav, f"{path}?p={page}", path, page)
            await nav.wait(TOPIC_TITLE)
            replies = await nav.page.locator(REPLY_CONTENT).evaluate_all(
                """(elements, limit) => elements.slice(0, limit + 1).map(element => {
                    const reply = element.closest('[id^="r_"]');
                    const number = Number(reply.querySelector('.no')?.innerText);
                    return {reply_id: Number(reply.id.slice(2)) || null,
                            number: Number.isInteger(number) && number > 0 ? number : null,
                            text: element.innerText.trim().slice(0, 12000)};
                })""",
                limit,
            )
            for reply in replies:
                reply["url"] = reply_url(nav.origin, topic_id, page, reply["reply_id"])
            next_page = await get_next_page(nav, path, page)
            await nav.check()
            return {
                "replies": replies[:limit], "page": page, "next_page": next_page,
                "truncated": len(replies) > limit,
            }

        return await self.run(read)


async def open_page(nav: Navigator, request: str, expected_path: str, page: int) -> None:
    await nav.open(request)
    current = urlsplit(nav.page.url)
    if current.path == "/signin":
        raise ForumError("v2ex_login_required", "此页面需要登录，可在本机配置官方 V2EX 个人访问令牌。")
    if current.path.rstrip("/") != expected_path or (page > 1 and f"p={page}" not in current.query.split("&")):
        raise ForumError("unexpected_page", "V2EX 返回了其他页面。")


async def get_next_page(nav: Navigator, path: str, page: int) -> int | None:
    exists = await nav.page.locator('#Main a.page_normal[href], #Main a[rel="next"][href]').evaluate_all(
        """(links, {path, next}) => links.some(link => {
            const url = new URL(link.href);
            return url.origin === location.origin && url.pathname === path && url.searchParams.get('p') === next;
        })""",
        {"path": path, "next": str(page + 1)},
    )
    return page + 1 if exists else None


async def page_topics(nav: Navigator, path: str, page: int, limit: int) -> dict:
    await open_page(nav, f"{path}?p={page}", path, page)
    await nav.wait(f"{TOPIC_LINKS}, #Main .empty")
    links = await nav.page.locator(TOPIC_LINKS).evaluate_all(
        "(links) => links.slice(0, 100).map(a => ({url: a.href, title: a.innerText.trim().slice(0, 300)}))"
    )
    topics = {}
    for link in links:
        url = urlsplit(link["url"])
        match = TOPIC_PATH.fullmatch(url.path)
        if match and link["title"] and same_origin(link["url"], nav.origin):
            topic_id = int(match[1])
            topics.setdefault(topic_id, {"topic_id": topic_id, "title": link["title"], "url": nav.origin + url.path})
    next_page = await get_next_page(nav, path, page)
    await nav.check()
    return {
        "topics": list(topics.values())[:limit], "page": page, "next_page": next_page,
        "truncated": len(topics) > limit,
    }


def unexpected_result() -> ForumError:
    return ForumError("page_unrecognized", "无法识别 V2EX API 返回的数据。")


def api_topic(item: object, origin: str) -> dict:
    if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 1:
        raise unexpected_result()
    topic_id = item["id"]
    title = clip_text(item.get("title"), 300)
    if not title:
        raise unexpected_result()
    return {"topic_id": topic_id, "title": title, "url": f"{origin}/t/{topic_id}"}


async def api_result(nav: Navigator, path: str, token: str, result_type: type):
    # A local, script-free document gives the browser a same-origin fetch context.
    # The bearer token is sent on this GET only. Redirects and cookies are disabled.
    async def local_document(route):
        await route.fulfill(
            content_type="text/html",
            headers={"Content-Security-Policy": "default-src 'none'; connect-src 'self'; img-src data:"},
            body='<title>V2EX API</title><link rel="icon" href="data:,">',
        )

    await nav.page.route(nav.origin + API_DOCUMENT, local_document, times=1)
    await nav.page.goto(nav.origin + API_DOCUMENT)
    await nav.throttle()
    response = await nav.page.evaluate(
        """async ({path, token}) => {
            const response = await fetch('/api/v2' + path, {
                headers: {Authorization: 'Bearer ' + token, Accept: 'application/json'},
                credentials: 'omit',
                cache: 'no-store',
                mode: 'same-origin',
                redirect: 'manual',
                signal: AbortSignal.timeout(12000),
            });
            const result = {status: response.status, data: null};
            if (!response.headers.get('content-type')?.toLowerCase().startsWith('application/json')) {
                return result;
            }
            const body = response.body;
            if (!body) return result;
            const reader = body.getReader();
            const decoder = new TextDecoder();
            let size = 0;
            let text = '';
            for (;;) {
                const {done, value} = await reader.read();
                if (done) break;
                size += value.length;
                if (size > 2097152) {
                    await reader.cancel();
                    return result;
                }
                text += decoder.decode(value, {stream: true});
            }
            try { result.data = JSON.parse(text + decoder.decode()); } catch {}
            return result;
        }""",
        {"path": path, "token": token},
    )
    await nav.check()
    status = response["status"]
    data = response["data"]
    if status == 0:
        raise ForumError("unexpected_page", "V2EX API 返回了重定向。")
    if status == 401 or (isinstance(data, dict) and data.get("message") == "Token expired"):
        raise ForumError("v2ex_token_rejected", "V2EX 个人访问令牌失效，请重新配置。")
    if status == 403:
        raise ForumError("site_blocked", "V2EX API 返回 HTTP 403，访问已暂停。")
    if status == 404:
        raise ForumError("not_found", "V2EX 没有找到请求的内容。")
    if status >= 400:
        raise ForumError("site_unavailable", f"V2EX API 返回 HTTP {status}。")
    if not isinstance(data, dict) or data.get("success") is not True or not isinstance(data.get("result"), result_type):
        raise unexpected_result()
    return data["result"]
