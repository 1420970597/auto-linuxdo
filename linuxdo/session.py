import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import time

from linuxdo.browser import Chrome, Navigator
from linuxdo.state import CookieConfig, ForumError, Hold, Store

GLOBAL_HOLDS = {"rate_limited", "site_blocked", "manual_verification", "unexpected_origin"}
LOG = logging.getLogger(__name__)


def iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


class Session:
    def __init__(self, chrome: Chrome, store: Store, refresh_seconds: int):
        self.chrome = chrome
        self.store = store
        self.refresh_seconds = refresh_seconds
        self.lock = asyncio.Lock()
        self.generation = None
        self.last_confirmed = None
        self.last_error = None
        self.next_check = 0.0

    def active_hold(self, config: CookieConfig) -> Hold | None:
        hold = self.store.hold()
        return hold if hold and hold.applies(config.generation) else None

    def status(self) -> dict:
        try:
            config = self.store.cookie_config()
        except ForumError as error:
            return {"state": error.code, "message": str(error)}
        if not config:
            return {"state": "cookies_required", "chrome_sandbox": self.chrome.sandbox_verified}
        hold = self.active_hold(config)
        result = {"state": "pending", "chrome_sandbox": self.chrome.sandbox_verified}
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
                if not config:
                    async with self.lock:
                        if self.store.cookie_config() is None:
                            await self.chrome.start("unconfigured")
                elif not self.active_hold(config) and (
                    config.generation != self.generation or time() >= self.next_check
                ):
                    await self.run("/latest")
            except ForumError:
                pass
            except Exception as error:
                LOG.error("Background session check failed (%s)", type(error).__name__)
            await asyncio.sleep(5)

    async def run(self, path: str, read: Callable[[Navigator], Awaitable[dict]] | None = None) -> dict:
        if self.lock.locked():
            raise ForumError("busy", "Chrome 正在处理另一项论坛请求，请稍后再试。")
        async with self.lock:
            config = self.store.cookie_config()
            if not config:
                raise ForumError("cookies_required", "请先导入 Linux.do 会话 Cookie。")
            hold = self.active_hold(config)
            if hold:
                raise hold.error()

            if self.generation != config.generation:
                self.generation = config.generation
                self.last_confirmed = None
            self.last_error = None
            # Browser reachability and account state are separate from content extraction.
            self.next_check = time() + 600
            try:
                try:
                    await self.chrome.start(config.generation, config.cookies)
                except Exception as error:
                    LOG.error("Chrome startup failed (%s)", type(error).__name__)
                    raise ForumError("browser_unavailable", "Chrome 沙箱未能启动。") from None

                async with self.chrome.page() as nav:
                    await nav.open(path)
                    if not await nav.authenticated():
                        raise ForumError("signed_out", "论坛会话已失效。")

                    self.last_confirmed = time()
                    self.next_check = self.last_confirmed + self.refresh_seconds
                    result = await read(nav) if read else {}
                    await nav.check()
            except ForumError as error:
                if error.code in GLOBAL_HOLDS:
                    self.store.set_hold(Hold(error.code, str(error), error.retry_at))
                elif error.code == "signed_out":
                    expired = Hold(
                        "cookies_expired",
                        "论坛会话已失效，请在 Chrome 重新登录并同步。",
                        generation=config.generation,
                    )
                    self.store.set_hold(expired)
                    raise expired.error() from None
                elif self.last_confirmed is None or not read:
                    self.last_error = error
                raise

            if self.store.cookie_config() != config:
                raise ForumError("cookies_changed", "论坛 Cookie 已更新，请重新查询。")
            return result
