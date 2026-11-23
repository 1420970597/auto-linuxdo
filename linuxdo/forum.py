import re
from urllib.parse import urlencode, urlsplit

from linuxdo.browser import Navigator, same_origin
from linuxdo.state import ForumError

LATEST_LINKS = ".topic-list-item .main-link a.raw-topic-link, .topic-list-item .main-link a.title"
SEARCH_LINKS = ".fps-result a.search-link, .search-results a.search-link"
EMPTY_SEARCH = ".no-results-container, .search-results .no-results, .no-search-results"
INVALID_SEARCH = ".search-notice .fps-invalid"
POSTS = "article[data-post-id] .cooked"
TOPIC_PATH = re.compile(r"^/t/(?:(?![0-9]+/)[^/]+/)?([1-9][0-9]{0,9})(?:/[1-9][0-9]{0,9})?/?$")


async def topic_links(nav: Navigator, selector: str, limit: int) -> list[dict]:
    links = await nav.page.locator(selector).evaluate_all(
        """(elements) => elements.filter(link => link.getClientRects().length).slice(0, 100)
           .map(link => ({title: link.innerText.trim().slice(0, 300), url: link.href}))"""
    )
    topics = {}
    for link in links:
        url = link["url"]
        path = urlsplit(url).path
        match = TOPIC_PATH.fullmatch(path)
        if not (match and link["title"] and same_origin(url, nav.origin)):
            continue
        topic_id = int(match[1])
        topics.setdefault(topic_id, {"topic_id": topic_id, "title": link["title"], "url": nav.origin + path})
        if len(topics) == limit:
            break
    return list(topics.values())


async def latest(nav: Navigator, limit: int) -> dict:
    await nav.wait(f"{LATEST_LINKS}, .topic-list .empty-topic-list, .empty-topic-list")
    return {"topics": await topic_links(nav, LATEST_LINKS, limit)}


async def search(nav: Navigator, query: str, limit: int) -> dict:
    await nav.wait(f"{SEARCH_LINKS}, {EMPTY_SEARCH}, {INVALID_SEARCH}")
    if await nav.visible(INVALID_SEARCH).is_visible():
        raise ForumError("query_rejected", "论坛未接受这个搜索词，请使用更具体的关键词。")
    return {"query": query, "topics": await topic_links(nav, SEARCH_LINKS, limit)}


def search_path(query: str) -> str:
    return "/search?" + urlencode({"q": query})


async def read_topic(nav: Navigator, topic_id: int, limit: int, post_number: int) -> dict:
    target = f"article:is(#post_{post_number}, #post-{post_number})[data-post-id] .cooked"
    await nav.wait(target)
    topic_path = urlsplit(nav.page.url).path
    match = TOPIC_PATH.fullmatch(topic_path)
    if not match or int(match[1]) != topic_id:
        raise ForumError("unexpected_topic", "论坛返回了其他主题。")
    title = await nav.visible(
        f"h1[data-topic-id='{topic_id}'] a.fancy-title, #topic-title h1, h1.fancy-title"
    ).inner_text()
    posts = await nav.page.locator(POSTS).evaluate_all(
        """(elements, {topicId, firstPost, limit}) => elements.flatMap(element => {
            const article = element.closest("article[data-post-id]");
            const number = article.id.match(/^post[_-](\\d+)$/)?.[1];
            if (!number || Number(number) < firstPost || article.dataset.topicId !== topicId) return [];
            if (!element.getClientRects().length) return [];
            const text = element.innerText.trim();
            if (!text) return [];
            return [{post_number: Number(number), text: text.slice(0, 12000), truncated: text.length > 12000}];
        }).slice(0, limit)""",
        {"topicId": str(topic_id), "firstPost": post_number, "limit": limit},
    )
    if not posts:
        raise ForumError("page_unrecognized", "未找到可识别的论坛帖子。")
    canonical = f"{nav.origin}/t/topic/{topic_id}"
    for post in posts:
        post["url"] = f'{canonical}/{post["post_number"]}'
    return {"topic_id": topic_id, "title": title.strip()[:300], "url": canonical, "posts": posts}
