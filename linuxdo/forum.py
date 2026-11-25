"""Linux.do reading through Discourse's JSON endpoints."""

import json
import re
from time import time
from urllib.parse import urlencode

from linuxdo.fetch import Fetch, Response
from linuxdo.html import text as html_text
from linuxdo.state import ForumError

ORIGIN = "https://linux.do"
LATEST = "/latest.json"
CATEGORIES = "/categories.json"
SEARCH = "/search.json?"
CURRENT_USER = "/session/current.json"
TEXT_LIMIT = 12_000
RATE_LIMIT_SECONDS = 900
CATEGORY_ORDERINGS = frozenset({"default", "created", "views", "posts", "likes", "activity"})
CATEGORY_CACHE_SECONDS = 3600

# ponytail: one process-wide cache, refreshed hourly; categories change rarely.
_category_cache: dict = {"at": 0.0, "entries": []}


def whole(value: object) -> int | None:
    return value if type(value) is int else None


def topic_entry(topic: dict) -> dict | None:
    topic_id = topic.get("id")
    if type(topic_id) is not int or topic_id < 1:
        return None
    title = topic.get("title") or topic.get("fancy_title") or ""
    if not isinstance(title, str) or not title.strip():
        return None
    return {
        "topic_id": topic_id,
        "title": title.strip()[:300],
        "url": f"{ORIGIN}/t/topic/{topic_id}",
    }


def topics_from(payload: dict, limit: int) -> list[dict]:
    topics = []
    seen = set()
    for topic in payload.get("topic_list", {}).get("topics", []):
        entry = topic_entry(topic) if isinstance(topic, dict) else None
        if entry and entry["topic_id"] not in seen:
            seen.add(entry["topic_id"])
            topics.append(entry)
        if len(topics) == limit:
            break
    return topics


def search_results(payload: dict, limit: int) -> list[dict]:
    """Search matches are posts; keep the topics they belong to, in result order."""
    titles = {}
    for topic in payload.get("topics", []):
        if isinstance(topic, dict) and type(topic.get("id")) is int:
            titles[topic["id"]] = topic.get("title") or topic.get("fancy_title") or ""

    order = []
    for post in payload.get("posts", []):
        if isinstance(post, dict):
            order.append(post.get("topic_id"))
    for topic in payload.get("topics", []):
        if isinstance(topic, dict):
            order.append(topic.get("id"))

    topics = []
    seen = set()
    for topic_id in order:
        if type(topic_id) is not int or topic_id < 1 or topic_id in seen:
            continue
        title = (titles.get(topic_id) or "").strip()
        if not title:
            continue
        seen.add(topic_id)
        topics.append({
            "topic_id": topic_id,
            "title": title[:300],
            "url": f"{ORIGIN}/t/topic/{topic_id}",
        })
        if len(topics) == limit:
            break
    return topics


def topic_posts(payload: dict, topic_id: int, limit: int, post_number: int) -> dict:
    topic = payload.get("post_stream", {})
    posts = []
    for post in topic.get("posts", []):
        if not isinstance(post, dict):
            continue
        number = post.get("post_number")
        if type(number) is not int or number < post_number:
            continue
        body = html_text(post.get("cooked") or "")
        if not body:
            continue
        posts.append({
            "post_number": number,
            "text": body[:TEXT_LIMIT],
            "truncated": len(body) > TEXT_LIMIT,
        })
        if len(posts) == limit:
            break
    if not posts:
        raise ForumError("page_unrecognized", "未找到可识别的论坛帖子。")
    title = payload.get("title") or payload.get("fancy_title") or ""
    canonical = f"{ORIGIN}/t/topic/{topic_id}"
    for post in posts:
        post["url"] = f'{canonical}/{post["post_number"]}'
    return {
        "topic_id": topic_id,
        "title": str(title).strip()[:300],
        "url": canonical,
        "posts": posts,
    }


def decode(response: Response, what: str) -> dict:
    if response.status == 429:
        raise ForumError("rate_limited", "论坛暂时限流。", time() + RATE_LIMIT_SECONDS)
    if response.status in (401, 403) and "not_logged_in" in response.text:
        raise ForumError("signed_out", "论坛会话已失效。")
    if response.status == 403:
        raise ForumError("site_blocked", "论坛返回 HTTP 403，访问已暂停。")
    if response.status == 404:
        raise ForumError("not_found", f"论坛没有找到{what}。")
    if response.status >= 400:
        raise ForumError("site_unavailable", f"论坛{what}返回 HTTP {response.status}。")
    try:
        payload = json.loads(response.text)
    except ValueError:
        raise ForumError("page_unrecognized", f"论坛{what}返回了无法识别的内容。") from None
    if not isinstance(payload, dict):
        raise ForumError("page_unrecognized", f"论坛{what}返回了无法识别的内容。")
    return payload


async def latest(fetch: Fetch, limit: int, cookies: dict[str, str]) -> dict:
    response = await fetch.get(ORIGIN + LATEST, cookies=cookies)
    return {"topics": topics_from(decode(response, "主题列表"), limit)}


def category_entry(category: dict) -> dict | None:
    category_id = whole(category.get("id"))
    slug = category.get("slug")
    name = category.get("name")
    if category_id is None or category_id < 1:
        return None
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", slug):
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    return {
        "id": category_id,
        "slug": slug,
        "name": name.strip()[:80],
        "topic_count": whole(category.get("topic_count")),
    }


def category_payload(payload: dict) -> list[dict]:
    entries = []
    for category in payload.get("category_list", {}).get("categories", []):
        if isinstance(category, dict):
            entry = category_entry(category)
            if entry:
                entries.append(entry)
    return entries


def resolve_category(entries: list[dict], wanted: str) -> dict:
    """Accept an id, an English slug, or a Chinese board name (unique partial ok)."""
    text = wanted.strip()
    if re.fullmatch(r"[0-9]{1,10}", text):
        for entry in entries:
            if entry["id"] == int(text):
                return entry
        raise ForumError("not_found", f"论坛没有 id 为 {text} 的板块。")
    lowered = text.lower()
    for entry in entries:
        if entry["slug"] == lowered or entry["name"] == text:
            return entry
    partial = [entry for entry in entries if text in entry["name"] or lowered in entry["slug"]]
    if len(partial) == 1:
        return partial[0]
    if partial:
        names = "、".join(f'{entry["name"]}({entry["slug"]})' for entry in partial)
        raise ForumError("ambiguous_category", f"板块名 {text} 匹配到多个：{names}。")
    names = "、".join(f'{entry["name"]}({entry["slug"]})' for entry in entries)
    raise ForumError("not_found", f"未找到板块 {text}。可用板块：{names}。")


def category_topics_from(
    payload: dict, limit: int, min_trust: int
) -> tuple[list[dict], int, int, bool]:
    """Return (topics, filtered_out, pinned, truncated); trust level comes with the listing."""
    users = {}
    for user in payload.get("users") or []:
        if isinstance(user, dict) and whole(user.get("id")) is not None:
            users[user["id"]] = user
    candidates, pinned = [], 0
    for topic in payload.get("topic_list", {}).get("topics", []):
        if not isinstance(topic, dict):
            continue
        if topic.get("pinned"):
            pinned += 1
            continue
        entry = topic_entry(topic)
        if entry:
            candidates.append((topic, entry))

    topics, filtered = [], 0
    for topic, base in candidates:
        posters = [item for item in topic.get("posters") or [] if isinstance(item, dict)]
        author_id = whole(posters[0].get("user_id")) if posters else None
        author = users.get(author_id) if author_id is not None else None
        trust = whole(author.get("trust_level")) if isinstance(author, dict) else None
        if min_trust and (trust is None or trust < min_trust):
            filtered += 1
            continue
        posts = whole(topic.get("posts_count")) or 0
        entry = dict(base)
        entry.update(
            author=(author or {}).get("username") if isinstance(author, dict) else None,
            author_trust_level=trust,
            reply_count=max(posts - 1, 0),
            views=whole(topic.get("views")),
            created_at=topic.get("created_at"),
        )
        topics.append(entry)
        if len(topics) == limit:
            break
    truncated = len(candidates) - filtered > len(topics)
    return topics, filtered, pinned, truncated


async def categories(fetch: Fetch, cookies: dict[str, str]) -> list[dict]:
    entries = _category_cache["entries"]
    if entries and time() - _category_cache["at"] < CATEGORY_CACHE_SECONDS:
        return list(entries)
    response = await fetch.get(ORIGIN + CATEGORIES, cookies=cookies)
    entries = category_payload(decode(response, "板块列表"))
    if entries:
        _category_cache.update(at=time(), entries=entries)
    return entries


async def category_topics(
    fetch: Fetch,
    category: str,
    order: str,
    page: int,
    limit: int,
    min_trust: int,
    cookies: dict[str, str],
) -> dict:
    if order not in CATEGORY_ORDERINGS:
        raise ForumError("bad_order", f"order 只能是 {'/'.join(sorted(CATEGORY_ORDERINGS))}。")
    chosen = resolve_category(await categories(fetch, cookies), category)
    path = f'/c/{chosen["slug"]}/{chosen["id"]}/l/latest.json?'
    response = await fetch.get(ORIGIN + path + urlencode({"page": page - 1, "order": order}), cookies=cookies)
    topics, filtered, pinned, truncated = category_topics_from(
        decode(response, "板块主题列表"), limit, min_trust
    )
    return {
        "category": chosen,
        "order": order,
        "page": page,
        "min_trust": min_trust,
        "topics": topics,
        "filtered_out": filtered,
        "skipped_pinned": pinned,
        "truncated": truncated,
    }


async def search(fetch: Fetch, query: str, limit: int, cookies: dict[str, str]) -> dict:
    response = await fetch.get(ORIGIN + SEARCH + urlencode({"q": query}), cookies=cookies)
    return {"query": query, "topics": search_results(decode(response, "搜索结果"), limit)}


async def read_topic(
    fetch: Fetch, topic_id: int, limit: int, post_number: int, cookies: dict[str, str]
) -> dict:
    path = f"/t/{topic_id}/{post_number}.json" if post_number > 1 else f"/t/{topic_id}.json"
    response = await fetch.get(ORIGIN + path, cookies=cookies)
    return topic_posts(decode(response, "主题"), topic_id, limit, post_number)


async def signed_in(fetch: Fetch, cookies: dict[str, str]) -> bool:
    response = await fetch.get(ORIGIN + CURRENT_USER, cookies=cookies)
    if response.status == 429:
        raise ForumError("rate_limited", "论坛暂时限流。", time() + RATE_LIMIT_SECONDS)
    if response.status == 403:
        raise ForumError("site_blocked", "论坛返回 HTTP 403，访问已暂停。")
    return response.status == 200
