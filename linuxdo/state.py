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
class Credentials:
    generation: str
    username: str = field(repr=False)
    password: str = field(repr=False)


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
        self.credentials_path = root / "credentials.json"
        self.hold_path = root / "forum-hold.json"
        self.token_path = root / "mcp-token"

    @contextmanager
    def service_lock(self):
        with self.root.joinpath("service.lock").open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def credentials(self) -> Credentials | None:
        try:
            return Credentials(**json.loads(self.credentials_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (ValueError, TypeError):
            raise ForumError("credentials_invalid", "请重新运行账号配置命令。") from None

    def configure(self, username: str, password: str) -> None:
        value = Credentials(uuid4().hex, username, password)
        write_private(self.credentials_path, json.dumps(asdict(value)))

    def token(self, *, create: bool = False) -> str:
        if create and not self.token_path.exists():
            write_private(self.token_path, secrets.token_urlsafe(32))
        return self.token_path.read_text(encoding="utf-8")

    def hold(self) -> Hold | None:
        try:
            return Hold(**json.loads(self.hold_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None

    def set_hold(self, value: Hold) -> None:
        write_private(self.hold_path, json.dumps(asdict(value)))

    def clear_hold(self) -> None:
        self.hold_path.unlink(missing_ok=True)
