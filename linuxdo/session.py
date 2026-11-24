import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import time

from linuxdo import forum
from linuxdo.fetch import Fetch, cookie_pairs
from linuxdo.state import CookieConfig, ForumError, Hold, Store

GLOBAL_HOLDS = {"rate_limited", "site_blocked", "manual_verification", "unexpected_origin"}
LOG = logging.getLogger(__name__)


def iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


class Session:
    def __init__(self, fetch: Fetch, store: Store, refresh_seconds: int):
        self.fetch = fetch
        self.store = store
        self.refresh_seconds = refresh_seconds
        self.lock = asyncio.Lock()
        self.generation = None
        self.last_confirmed = None
        self.last_error = None
        self.next_check = 0.0

    def active_hold(self, config: CookieConfig | None) -> Hold | None:
        hold = self.store.hold()
        return hold if hold and hold.applies(config.generation if config else None) else None

    def status(self) -> dict:
        try:
            config = self.store.cookie_config()
        except ForumError as error:
            return {"state": error.code, "message": str(error)}
        if not config:
            return {"state": "anonymous", "transport": self.fetch.ready()}
        hold = self.active_hold(config)
        result = {"state": "pending", "transport": self.fetch.ready()}
        if hold:
            result.update(state=hold.code, message=hold.message)
            if hold.retry_at:
                result["retry_at"] = iso_timestamp(hold.retry_at)
        elif config.generation == self.generation:
            if self.last_error:
                result.update(state=self.last_error.code, message=str(self.last_error))
            elif self.last_confirmed:
                result.update(state="authenticated", last_confirmed=iso_timestamp(self.last_confirmed))
        return result

    async def maintain(self) -> None:
        while True:
            try:
                config = self.store.cookie_config()
                if config and not self.active_hold(config) and (
                    config.generation != self.generation or time() >= self.next_check
                ):
                    await self.run(lambda fetch, cookies: forum.latest(fetch, 1, cookies))
            except ForumError:
                pass
            except Exception as error:
                LOG.error("Background session check failed (%s)", type(error).__name__)
            await asyncio.sleep(5)

    async def run(self, read: Callable[[Fetch, dict[str, str]], Awaitable[dict]]) -> dict:
        if self.lock.locked():
            raise ForumError("busy", "论坛传输组件正在处理另一项请求，请稍后再试。")
        async with self.lock:
            config = self.store.cookie_config()
            hold = self.active_hold(config)
            if hold:
                raise hold.error()

            # Without a synced session the public parts of Linux.do still read fine; only
            # endpoints that require login (search) report `signed_out`.
            generation = config.generation if config else None
            if self.generation != generation:
                self.generation = generation
                self.last_confirmed = None
                self.next_check = 0.0
            self.last_error = None
            cookies = cookie_pairs(config.cookies) if config else {}
            try:
                if cookies and (self.next_check == 0.0 or time() >= self.next_check):
                    if not await forum.signed_in(self.fetch, cookies):
                        raise ForumError("signed_out", "论坛会话已失效。")
                    self.last_confirmed = time()
                    self.next_check = self.last_confirmed + self.refresh_seconds
                result = await read(self.fetch, cookies)
            except ForumError as error:
                if error.code in GLOBAL_HOLDS:
                    self.store.set_hold(Hold(error.code, str(error), error.retry_at))
                elif error.code == "signed_out" and config is None:
                    raise ForumError(
                        "cookies_required", "该操作需要登录，请先同步 Linux.do 会话 Cookie。"
                    ) from None
                elif error.code == "signed_out":
                    expired = Hold(
                        "cookies_expired",
                        "论坛会话已失效，请在 Chrome 重新登录并同步。",
                        generation=config.generation,
                    )
                    self.store.set_hold(expired)
                    raise expired.error() from None
                elif self.last_confirmed is None:
                    self.last_error = error
                raise

            if self.store.cookie_config() != config:
                raise ForumError("cookies_changed", "论坛 Cookie 已更新，请重新查询。")
            return result
