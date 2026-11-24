"""Linux.do reading through Discourse's JSON endpoints."""

import json
from time import time
from urllib.parse import urlencode

from linuxdo.fetch import Fetch, Response
from linuxdo.html import text as html_text
from linuxdo.state import ForumError

ORIGIN = "https://linux.do"
LATEST = "/latest.json"
SEARCH = "/search.json?"
CURRENT_USER = "/session/current.json"
TEXT_LIMIT = 12_000
RATE_LIMIT_SECONDS = 900


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
