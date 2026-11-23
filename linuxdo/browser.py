import asyncio
import re
import shutil
from contextlib import asynccontextmanager, suppress
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import time
from urllib.parse import urljoin, urlsplit

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    Response,
    TimeoutError as PlaywrightTimeout,
)

from linuxdo.state import Credentials, ForumError, write_private

ORIGIN = "https://linux.do"
AUTHENTICATED = ".d-header #current-user, .d-header .header-dropdown-toggle.current-user"
SIGNED_OUT = ".d-header .login-button, #login-account-name"
ACCOUNT = "input#login-account-name"
PASSWORD = "input#login-account-password"
SUBMIT = "button#login-button"
LOGIN_ERROR = ".login-fullpage .alert-error, #login-alert, .login-error"
VERIFICATION = (
    'input[autocomplete="one-time-code"], #second-factor, #login-second-factor, '
    '#security-key-authenticate-button, iframe[src*="challenges.cloudflare.com"], '
    '#challenge-form, #challenge-stage, #cf-challenge-running, [id^="cf-chl-widget"]'
)


def retry_after(value: str | None) -> float:
    now = time()
    if value:
        try:
            if value.isascii() and value.isdecimal():
                return now + max(60, int(value))
            parsed = parsedate_to_datetime(value)
            return max(now + 60, parsed.timestamp())
        except (OverflowError, TypeError, ValueError):
            pass
    return now + 900


def same_origin(url: str, origin: str) -> bool:
    parsed = urlsplit(url)
    expected = urlsplit(origin)
    return (parsed.scheme, parsed.netloc) == (expected.scheme, expected.netloc)


class Navigator:
    def __init__(self, page: Page, origin: str, throttle):
        self.page = page
        self.origin = origin
        self.throttle = throttle
        self.response_error = None
        page.on("response", self.observe)

    def observe(self, response: Response) -> None:
        if not same_origin(response.url, self.origin):
            return
        if response.status == 429:
            deadline = retry_after(response.headers.get("retry-after"))
            self.response_error = ForumError("rate_limited", "论坛暂时限流。", deadline)
        elif self.response_error is None and response.headers.get("cf-mitigated") == "challenge":
            self.response_error = ForumError("manual_verification", "论坛要求人工验证。")

    def visible(self, selector: str):
        return self.page.locator(selector).filter(visible=True).first

    async def check(self) -> None:
        if self.response_error:
            raise self.response_error
        if not same_origin(self.page.url, self.origin):
            raise ForumError("unexpected_origin", "页面已离开论坛域名。")
        if await self.visible(VERIFICATION).is_visible():
            raise ForumError("manual_verification", "论坛要求人工验证。")

    async def wait(self, selector: str, timeout: int = 12_000) -> None:
        try:
            await self.visible(f"{selector}, {VERIFICATION}").wait_for(timeout=timeout)
        except PlaywrightTimeout:
            await self.check()
            raise ForumError("page_unrecognized", "未找到预期的论坛页面元素。") from None
        await self.check()

    async def open(self, path: str) -> None:
        await self.throttle()
        if self.response_error:
            raise self.response_error
        response = await self.page.goto(self.origin + path, wait_until="domcontentloaded")
        await self.check()
        if response is None:
            raise ForumError("site_unavailable", "论坛没有返回页面。")
        if response.status == 401:
            raise ForumError("signed_out", "论坛会话已过期。")
        if response.status == 403:
            raise ForumError("site_blocked", "论坛返回 HTTP 403，访问已暂停。")
        if response.status == 404:
            raise ForumError("not_found", "论坛没有找到请求的页面。")
        if response.status >= 400:
            raise ForumError("site_unavailable", f"论坛页面返回 HTTP {response.status}。")

    async def authenticated(self) -> bool:
        await self.wait(f"{AUTHENTICATED}, {SIGNED_OUT}")
        return await self.visible(AUTHENTICATED).is_visible()

    async def login(self, credentials: Credentials, before_submit) -> None:
        await self.open("/login")
        await self.wait(f"{ACCOUNT}, {AUTHENTICATED}")
        if await self.visible(AUTHENTICATED).is_visible():
            return

        account = self.visible(ACCOUNT)
        password = self.visible(PASSWORD)
        submit = self.visible(SUBMIT)
        await self.wait(PASSWORD)
        await self.wait(SUBMIT)
        for field in (account, password, submit):
            action = await field.evaluate(
                "(field) => field.getAttribute('formaction') || field.form?.getAttribute('action') || ''"
            )
            if not same_origin(urljoin(self.page.url, action), self.origin):
                raise ForumError("unexpected_origin", "登录表单已离开论坛域名。")

        await self.check()
        await account.fill(credentials.username)
        await password.fill(credentials.password)
        await self.throttle()
        await self.check()
        before_submit()
        await submit.click()
        await self.wait(f"{AUTHENTICATED}, {LOGIN_ERROR}", timeout=15_000)
        if not await self.visible(AUTHENTICATED).is_visible():
            raise ForumError("login_rejected", "论坛未接受登录，请检查账号或在论坛完成验证。")


class Chrome:
    def __init__(
        self, playwright: Playwright, state_dir: Path, *, origin: str = ORIGIN, spacing: float = 4
    ):
        self.playwright = playwright
        self.state_dir = state_dir
        self.origin = origin
        self.spacing = spacing
        self.next_navigation = 0.0
        self.context: BrowserContext | None = None
        self.generation: str | None = None
        self.sandbox_verified = False

    async def start(self, generation: str) -> None:
        if self.context and self.generation == generation:
            return
        await self.close()

        profile = self.state_dir / "chrome"
        owner = self.state_dir / "chrome-generation"
        previous = owner.read_text(encoding="utf-8") if owner.exists() else None
        if previous != generation:
            if profile.exists():
                shutil.rmtree(profile)
            write_private(owner, generation)
        profile.mkdir(mode=0o700, exist_ok=True)

        context = await self.playwright.chromium.launch_persistent_context(
            profile,
            channel="chrome",
            chromium_sandbox=True,
            ignore_default_args=[
                "--disable-client-side-phishing-detection",
                "--disable-dev-shm-usage",
                "--enable-unsafe-swiftshader",
                "--unsafely-disable-devtools-self-xss-warnings",
            ],
            accept_downloads=False,
            headless=True,
            timeout=20_000,
        )
        self.context = context
        self.generation = generation
        context.on("close", lambda: self.reset(context))
        try:
            context.set_default_timeout(12_000)
            probe = context.pages[0] if context.pages else await context.new_page()
            await probe.goto("chrome://sandbox", wait_until="domcontentloaded")
            status = await probe.locator("body").inner_text()
            required = (
                r"Layer 1 Sandbox[^\S\n]+Namespace",
                r"PID namespaces[^\S\n]+Yes",
                r"Network namespaces[^\S\n]+Yes",
                r"Seccomp-BPF sandbox[^\S\n]+Yes",
            )
            if not all(re.search(pattern + r"\b", status, re.I) for pattern in required):
                raise RuntimeError("Chrome sandbox verification failed (namespace and seccomp required)")
            await probe.goto("about:blank")
        except BaseException:
            await self.close()
            raise
        self.sandbox_verified = True

    def reset(self, context: BrowserContext) -> None:
        if self.context is context:
            self.context = None
            self.sandbox_verified = False

    async def close(self) -> None:
        context = self.context
        if context:
            self.reset(context)
            await context.close()

    async def throttle(self) -> None:
        clock = asyncio.get_running_loop().time
        await asyncio.sleep(max(0, self.next_navigation - clock()))
        self.next_navigation = clock() + self.spacing

    @asynccontextmanager
    async def page(self):
        if self.context is None or not self.sandbox_verified:
            raise ForumError("browser_unavailable", "Chrome 沙箱尚未就绪。")
        page = None
        try:
            page = await self.context.new_page()
            yield Navigator(page, self.origin, self.throttle)
        except PlaywrightError:
            raise ForumError("browser_error", "Chrome 无法完成当前页面操作。") from None
        finally:
            if page and not page.is_closed():
                with suppress(PlaywrightError):
                    await page.close()
