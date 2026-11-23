import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import time

from linuxdo.browser import Chrome, Navigator
from linuxdo.state import Credentials, ForumError, Hold, Store

GLOBAL_HOLDS = {"rate_limited", "site_blocked", "manual_verification", "unexpected_origin"}
ACCOUNT_HOLDS = {"login_rejected", "login_flow_unrecognized"}
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

    def active_hold(self, credentials: Credentials) -> Hold | None:
        hold = self.store.hold()
        return hold if hold and hold.applies(credentials.generation) else None

    def status(self) -> dict:
        try:
            credentials = self.store.credentials()
        except ForumError as error:
            return {"state": error.code, "message": str(error)}
        if not credentials:
            return {"state": "credentials_required", "chrome_sandbox": self.chrome.sandbox_verified}
        hold = self.active_hold(credentials)
        result = {"state": "pending", "chrome_sandbox": self.chrome.sandbox_verified}
        if hold:
            result.update(state=hold.code, message=hold.message)
            if hold.retry_at:
                result["retry_at"] = iso_timestamp(hold.retry_at)
        elif credentials.generation == self.generation:
            if self.last_error:
                result.update(state=self.last_error.code, message=str(self.last_error))
            elif self.last_confirmed:
                result.update(state="authenticated", last_confirmed=iso_timestamp(self.last_confirmed))
        return result

    async def maintain(self) -> None:
        while True:
            try:
                credentials = self.store.credentials()
                if not credentials:
                    async with self.lock:
                        if self.store.credentials() is None:
                            await self.chrome.start("unconfigured")
                elif not self.active_hold(credentials) and (
                    credentials.generation != self.generation or time() >= self.next_check
                ):
                    await self.run("/latest")
            except ForumError:
                pass
            except Exception as error:
                LOG.error("Background session check failed (%s)", type(error).__name__)
            await asyncio.sleep(5)

    async def visit(self, nav: Navigator, path: str) -> bool:
        try:
            await nav.open(path)
        except ForumError as error:
            if error.code == "signed_out":
                return False
            raise
        return await nav.authenticated()

    async def sign_in(self, nav: Navigator, credentials: Credentials) -> None:
        def record_submit() -> None:
            if self.store.credentials() != credentials:
                raise ForumError("credentials_changed", "论坛账号已更新，请重新查询。")
            self.store.set_hold(Hold(
                "login_unconfirmed",
                "登录提交后的状态未确认，请检查论坛后再恢复。",
                generation=credentials.generation,
            ))

        try:
            await nav.login(credentials, record_submit)
        except ForumError as error:
            if error.code == "page_unrecognized" and not self.active_hold(credentials):
                raise ForumError("login_flow_unrecognized", "未识别论坛的账号密码登录页面。") from None
            raise
        hold = self.active_hold(credentials)
        if hold and hold.code == "login_unconfirmed":
            self.store.clear_hold()

    async def run(self, path: str, read: Callable[[Navigator], Awaitable[dict]] | None = None) -> dict:
        if self.lock.locked():
            raise ForumError("busy", "Chrome 正在处理另一项论坛请求，请稍后再试。")
        async with self.lock:
            credentials = self.store.credentials()
            if not credentials:
                raise ForumError("credentials_required", "请先在 Docker 内配置 Linux.do 账号。")
            hold = self.active_hold(credentials)
            if hold:
                raise hold.error()

            if self.generation != credentials.generation:
                self.generation = credentials.generation
                self.last_confirmed = None
            self.last_error = None
            # Browser reachability and account state are separate from content extraction.
            self.next_check = time() + 600
            try:
                try:
                    await self.chrome.start(credentials.generation)
                except Exception as error:
                    LOG.error("Chrome startup failed (%s)", type(error).__name__)
                    raise ForumError("browser_unavailable", "Chrome 沙箱未能启动。") from None

                async with self.chrome.page() as nav:
                    if not await self.visit(nav, path):
                        await self.sign_in(nav, credentials)
                        if not await self.visit(nav, path):
                            raise ForumError("login_rejected", "论坛未保留登录会话。")

                    self.last_confirmed = time()
                    self.next_check = self.last_confirmed + self.refresh_seconds
                    result = await read(nav) if read else {}
                    await nav.check()
            except ForumError as error:
                if error.code in GLOBAL_HOLDS:
                    self.store.set_hold(Hold(error.code, str(error), error.retry_at))
                elif error.code in ACCOUNT_HOLDS:
                    self.store.set_hold(Hold(error.code, str(error), generation=credentials.generation))
                elif self.last_confirmed is None or not read:
                    self.last_error = error
                raise

            if self.store.credentials() != credentials:
                raise ForumError("credentials_changed", "论坛账号已更新，请重新查询。")
            return result
