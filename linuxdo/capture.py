import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_CAPTURE_BYTES = 256 * 1024
CAPTURE_TOKEN_FILE = "page-capture-token"
Forum = Literal["linuxdo", "v2ex"]
PageKind = Literal["list", "topic"]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
PageURL = Annotated[str, StringConstraints(max_length=2048)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=6000)]
ORIGINS = {"linuxdo": "linux.do", "v2ex": "www.v2ex.com"}
TOPIC_PATHS = {
    "linuxdo": re.compile(r"/t/(?:[\w%-]+/)?[1-9][0-9]*(?:/[1-9][0-9]*)?", re.ASCII),
    "v2ex": re.compile(r"/t/[1-9][0-9]*"),
}
LIST_PATHS = {
    "linuxdo": re.compile(r"/(?:latest|top|new|unread|categories|search|hot|c/[\w%/-]+)?", re.ASCII),
    "v2ex": re.compile(r"/(?:recent|go/[\w-]+)?", re.ASCII),
}


def normalize_url(value: str) -> tuple[Forum, PageKind, str]:
    parsed = urlsplit(value)
    host = parsed.netloc
    if parsed.scheme != "https" or host not in ("linux.do", "www.v2ex.com", "v2ex.com"):
        raise ValueError("unsupported forum origin")
    forum = "linuxdo" if host == "linux.do" else "v2ex"
    path = parsed.path.rstrip("/") or "/"
    if TOPIC_PATHS[forum].fullmatch(path):
        kind = "topic"
    elif LIST_PATHS[forum].fullmatch(path):
        kind = "list"
    else:
        raise ValueError("unsupported forum page")
    query = []
    params = parse_qs(parsed.query, max_num_fields=64)
    for name in ("page", "p"):
        if name in params and re.fullmatch(r"[1-9][0-9]{0,5}", params[name][0]):
            query.append((name, params[name][0]))
    for name, applicable in (("q", forum == "linuxdo" and path == "/search"),
                             ("tab", forum == "v2ex" and path == "/")):
        if applicable and name in params:
            query.append((name, params[name][0][:160]))
    suffix = "?" + urlencode(query) if query else ""
    return forum, kind, f"https://{ORIGINS[forum]}{path}{suffix}"


class TopicLink(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: Title
    url: PageURL


class CapturedPost(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    number: Annotated[int, Field(ge=1, le=2_147_483_647)] | None = None
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]


class CapturedPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: PageURL
    title: Title
    body: Text = ""
    links: Annotated[list[TopicLink], Field(max_length=25)] = Field(default_factory=list)
    posts: Annotated[list[CapturedPost], Field(max_length=8)] = Field(default_factory=list)

    def render(self) -> tuple[Forum, PageKind, dict]:
        forum, kind, url = normalize_url(self.url)
        links = {}
        for link in self.links:
            source_forum, target_kind, target_url = normalize_url(link.url)
            if source_forum != forum or target_kind != "topic":
                raise ValueError("unsupported topic link")
            links.setdefault(target_url, {"title": link.title, "url": target_url})
        if kind == "list" and (not links or self.body or self.posts):
            raise ValueError("unrecognized list")
        if kind == "topic" and (links or not (self.body or self.posts)):
            raise ValueError("unrecognized topic")
        return forum, kind, {
            "title": self.title,
            "body": self.body,
            "topics": list(links.values()),
            "posts": [post.model_dump() for post in self.posts],
            "url": url,
        }


class Captures:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS pages (
                    url TEXT PRIMARY KEY, forum TEXT NOT NULL, kind TEXT NOT NULL,
                    title TEXT NOT NULL, captured_at TEXT NOT NULL, content TEXT NOT NULL
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS pages_forum_updated ON pages(forum, captured_at DESC)")

    def save(self, page: CapturedPage) -> dict:
        forum, kind, data = page.render()
        captured_at = datetime.now(UTC).isoformat()
        summary = {"forum": forum, "kind": kind, "url": data["url"], "title": data["title"],
                   "captured_at": captured_at}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                """INSERT INTO pages(url, forum, kind, title, captured_at, content)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                       captured_at=excluded.captured_at, content=excluded.content""",
                (data["url"], forum, kind, data["title"], captured_at, json.dumps(data, ensure_ascii=False)),
            )
        return summary

    def list(self, forum: Forum | None, limit: int) -> list[dict]:
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            query = "SELECT url, forum, kind, title, captured_at FROM pages"
            if forum:
                rows = db.execute(query + " WHERE forum=? ORDER BY captured_at DESC, url LIMIT ?", (forum, limit))
            else:
                rows = db.execute(query + " ORDER BY captured_at DESC, url LIMIT ?", (limit,))
            return [dict(row) for row in rows]

    def read(self, url: str) -> dict | None:
        _, _, key = normalize_url(url)
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT forum, kind, captured_at, content FROM pages WHERE url=?", (key,)
            ).fetchone()
        if row is None:
            return None
        return {"forum": row[0], "kind": row[1], "captured_at": row[2], **json.loads(row[3])}
