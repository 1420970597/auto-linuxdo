import fcntl
import json
import os
import secrets
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time
from uuid import uuid4

STATE_DIR = Path("/var/lib/linuxdo")


class ForumError(Exception):
    def __init__(self, code: str, message: str, retry_at: float | None = None):
        super().__init__(message)
        self.code = code
        self.retry_at = retry_at


@dataclass(frozen=True)
class CookieConfig:
    generation: str
    source: str
    cookies: list[dict] = field(repr=False)


@dataclass(frozen=True)
class Hold:
    code: str
    message: str
    retry_at: float | None = None
    generation: str | None = None

    def applies(self, generation: str) -> bool:
        same_account = self.generation in (None, generation)
        return same_account and (self.retry_at is None or self.retry_at > time())

    def error(self) -> ForumError:
        return ForumError(self.code, self.message, self.retry_at)


def write_private(path: Path, value: str) -> None:
    with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as file:
        temp_path = Path(file.name)
        try:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path = STATE_DIR):
        self.root = root
        self.cookies_path = root / "cookies.json"
        self.legacy_credentials_path = root / "credentials.json"
        self.hold_path = root / "forum-hold.json"
        self.token_path = root / "mcp-token"
        self.sync_token_path = root / "cookie-sync-token"

    @contextmanager
    def service_lock(self):
        with self.root.joinpath("service.lock").open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    @contextmanager
    def config_lock(self):
        with self.root.joinpath("config.lock").open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            yield

    def cookie_config(self) -> CookieConfig | None:
        try:
            return CookieConfig(**json.loads(self.cookies_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (ValueError, TypeError):
            raise ForumError("cookies_invalid", "请重新导入论坛会话 Cookie。") from None

    def configure(self, cookies: list[dict], *, source: str = "manual") -> bool:
        with self.config_lock():
            try:
                current = self.cookie_config()
            except ForumError:
                current = None
            if current and current.cookies == cookies and current.source == source:
                return False
            value = CookieConfig(uuid4().hex, source, cookies)
            write_private(self.cookies_path, json.dumps(asdict(value)))
            return True

    def forget(self, *, source: str | None = None) -> bool:
        with self.config_lock():
            if source is not None:
                current = self.cookie_config()
                if not current or current.source != source:
                    return False
            try:
                self.cookies_path.unlink()
            except FileNotFoundError:
                return False
            return True

    def token(self, *, create: bool = False, sync: bool = False) -> str:
        path = self.sync_token_path if sync else self.token_path
        if create and not path.exists():
            write_private(path, secrets.token_urlsafe(32))
        return path.read_text(encoding="utf-8")

    def hold(self) -> Hold | None:
        try:
            return Hold(**json.loads(self.hold_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None

    def set_hold(self, value: Hold) -> None:
        write_private(self.hold_path, json.dumps(asdict(value)))

    def clear_hold(self) -> None:
        self.hold_path.unlink(missing_ok=True)
