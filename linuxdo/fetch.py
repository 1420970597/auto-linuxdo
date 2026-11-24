"""Page transport backed by the cfsolve `forum-fetch` binary.

The binary impersonates the profile's browser, reuses a `cf_clearance` cookie between runs, and
solves a Cloudflare challenge when the site serves one. One process per request keeps the
service free of a long-lived browser; the cookie file is the only shared state.
"""

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from linuxdo.state import ForumError, write_private

DEFAULT_BINARY = "/usr/local/bin/forum-fetch"
DEFAULT_PROFILE = "/usr/local/share/cfsolve/safari-26-ios.json"
FETCH_TIMEOUT = 240
JAR_NAME = "cf-cookies.json"


def same_origin(url: str, origin: str) -> bool:
    parsed = urlsplit(url)
    expected = urlsplit(origin)
    return (parsed.scheme, parsed.netloc) == (expected.scheme, expected.netloc)


def cookie_pairs(entries: list[dict]) -> dict[str, str]:
    """Cookie config entries as name/value pairs, skipping malformed ones."""
    pairs = {}
    for entry in entries:
        name, value = entry.get("name"), entry.get("value")
        if isinstance(name, str) and isinstance(value, str):
            pairs[name] = value
    return pairs


class Response:
    def __init__(self, url: str, status: int, text: str, solved: bool):
        self.url = url
        self.status = status
        self.text = text
        self.solved = solved


class Fetch:
    def __init__(
        self,
        root: Path,
        *,
        binary: str | None = None,
        profile: str | None = None,
        proxy: str | None = None,
    ):
        self.jar_path = root / JAR_NAME
        self.binary = Path(binary or os.environ.get("LINUXDO_FETCH_BINARY", DEFAULT_BINARY))
        self.profile = Path(profile or os.environ.get("LINUXDO_FETCH_PROFILE", DEFAULT_PROFILE))
        self.proxy = proxy or os.environ.get("LINUXDO_FETCH_PROXY")
        self.lock = asyncio.Lock()

    def ready(self) -> bool:
        return (
            self.binary.is_file()
            and os.access(self.binary, os.X_OK)
            and self.profile.is_file()
        )

    def _jar(self) -> dict[str, str]:
        try:
            jar = json.loads(self.jar_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return jar if isinstance(jar, dict) else {}

    def _write_jar(self, jar: dict[str, str]) -> None:
        write_private(self.jar_path, json.dumps(jar, sort_keys=True))

    async def get(self, url: str, *, cookies: dict[str, str] | None = None) -> Response:
        if not self.ready():
            raise ForumError("transport_unavailable", "论坛传输组件尚未就绪。")
        async with self.lock:
            jar = self._jar()
            # The synced session cookies win over a stale copy left in the clearance jar.
            jar.update(cookies or {})
            self._write_jar(jar)
            command = [
                str(self.binary),
                "--profile",
                str(self.profile),
                "--url",
                url,
                "--cookie-file",
                str(self.jar_path),
            ]
            if self.proxy:
                command += ["--proxy", self.proxy]
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError:
                raise ForumError("transport_unavailable", "无法启动论坛传输组件。") from None
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), FETCH_TIMEOUT)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise ForumError("site_unavailable", "论坛请求超时。") from None

            try:
                payload = json.loads(stdout)
            except ValueError:
                detail = stderr.decode("utf-8", "replace").strip().splitlines()
                raise ForumError(
                    "site_unavailable",
                    f"论坛传输组件失败：{detail[-1] if detail else '无输出'}",
                ) from None
            if not isinstance(payload, dict):
                raise ForumError("site_unavailable", "论坛传输组件返回了无法识别的结果。")
            if "error" in payload:
                message = str(payload["error"])[:200]
                code = "site_blocked" if "challenge" in message.lower() else "site_unavailable"
                raise ForumError(code, f"论坛请求失败：{message}")

            cookies = payload.get("cookies")
            if isinstance(cookies, dict):
                self._write_jar({str(k): str(v) for k, v in cookies.items()})
            return Response(
                url=str(payload.get("url", url)),
                status=int(payload.get("status", 0)),
                text=str(payload.get("html", "")),
                solved=bool(payload.get("solved", False)),
            )
