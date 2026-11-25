import unittest

from linuxdo.fetch import Response
from linuxdo.forum import (
    category_topics_from,
    decode,
    resolve_category,
    search_results,
    topic_posts,
    topics_from,
)
from linuxdo.state import ForumError


def response(payload: str, status: int = 200) -> Response:
    return Response(url="https://linux.do/x", status=status, text=payload, solved=False)


class TopicListTests(unittest.TestCase):
    def test_topics_keep_order_and_skip_broken_entries(self):
        payload = {
            "topic_list": {
                "topics": [
                    {"id": 3, "title": "三"},
                    {"id": "4", "title": "字符串 ID"},
                    {"id": 5, "title": "  "},
                    {"id": 3, "title": "重复"},
                    {"id": 7, "fancy_title": "七"},
                ]
            }
        }
        self.assertEqual(
            topics_from(payload, 10),
            [
                {"topic_id": 3, "title": "三", "url": "https://linux.do/t/topic/3"},
                {"topic_id": 7, "title": "七", "url": "https://linux.do/t/topic/7"},
            ],
        )
        self.assertEqual(len(topics_from(payload, 1)), 1)

    def test_search_orders_by_post_then_remaining_topics(self):
        payload = {
            "posts": [{"topic_id": 9}, {"topic_id": 2}],
            "topics": [
                {"id": 2, "title": "二"},
                {"id": 9, "title": "九"},
                {"id": 4, "title": "四"},
            ],
        }
        self.assertEqual([t["topic_id"] for t in search_results(payload, 10)], [9, 2, 4])


class TopicPostsTests(unittest.TestCase):
    def test_posts_render_text_from_cooked_html(self):
        payload = {
            "title": "标题",
            "post_stream": {
                "posts": [
                    {"post_number": 1, "cooked": "<p>第一段</p><p>第二段</p>"},
                    {"post_number": 2, "cooked": "<p>   </p>"},
                    {"post_number": 3, "cooked": "<p>第三段</p>"},
                ]
            },
        }
        result = topic_posts(payload, 42, 5, 1)
        self.assertEqual(result["title"], "标题")
        self.assertEqual(result["url"], "https://linux.do/t/topic/42")
        self.assertEqual([p["post_number"] for p in result["posts"]], [1, 3])
        self.assertEqual(result["posts"][0]["text"], "第一段\n第二段")
        self.assertEqual(result["posts"][0]["url"], "https://linux.do/t/topic/42/1")

    def test_post_number_skips_earlier_posts(self):
        payload = {"post_stream": {"posts": [
            {"post_number": 1, "cooked": "<p>一</p>"},
            {"post_number": 4, "cooked": "<p>四</p>"},
        ]}}
        self.assertEqual(
            [p["post_number"] for p in topic_posts(payload, 1, 5, 3)["posts"]], [4]
        )

    def test_empty_post_stream_is_reported(self):
        with self.assertRaises(ForumError) as caught:
            topic_posts({"post_stream": {"posts": []}}, 1, 5, 1)
        self.assertEqual(caught.exception.code, "page_unrecognized")


class CategoryTests(unittest.TestCase):
    ENTRIES = [
        {"id": 36, "slug": "welfare", "name": "福利羊毛", "topic_count": 20166},
        {"id": 11, "slug": "gossip", "name": "搞七捻三", "topic_count": 280693},
        {"id": 4, "slug": "develop", "name": "开发调优", "topic_count": 130296},
        {"id": 34, "slug": "news", "name": "前沿快讯", "topic_count": 28660},
    ]

    def test_resolve_by_slug_name_id_and_unique_partial(self):
        for wanted in ("welfare", "Welfare", "福利羊毛", "36", " 福利 ".strip()):
            self.assertEqual(resolve_category(self.ENTRIES, wanted)["id"], 36, wanted)
        self.assertEqual(resolve_category(self.ENTRIES, "羊毛")["id"], 36)

    def test_resolve_rejects_unknown_and_ambiguous(self):
        with self.assertRaises(ForumError) as caught:
            resolve_category(self.ENTRIES + [{"id": 90, "slug": "welfare2", "name": "福利兑换"}], "福利")
        self.assertEqual(caught.exception.code, "ambiguous_category")

        with self.assertRaises(ForumError) as caught:
            resolve_category(self.ENTRIES, "不存在的板块")
        self.assertIn("可用板块", str(caught.exception))
        self.assertEqual(caught.exception.code, "not_found")

    def test_topics_carry_trust_level_and_skip_pinned(self):
        payload = {
            "users": [
                {"id": 100, "username": "alice", "trust_level": 3},
                {"id": 200, "username": "bob", "trust_level": 1},
                {"id": 300, "username": "carol"},
            ],
            "topic_list": {
                "topics": [
                    {"id": 1, "title": "置顶", "pinned": True},
                    {"id": 2, "title": "低等级", "posters": [{"user_id": 200}], "posts_count": 3},
                    {"id": 3, "title": "高等级", "posters": [{"user_id": 100}],
                     "posts_count": 5, "views": 42, "created_at": "2026-11-25T01:33:32.580Z"},
                    {"id": 4, "title": "等级未知", "posters": [{"user_id": 300}], "posts_count": 1},
                ]
            },
        }
        topics, filtered, pinned, truncated = category_topics_from(payload, 10, 0)
        self.assertEqual([t["topic_id"] for t in topics], [2, 3, 4])
        self.assertEqual([t["author_trust_level"] for t in topics], [1, 3, None])
        self.assertEqual((filtered, pinned), (0, 1))
        self.assertEqual(topics[1]["reply_count"], 4)
        self.assertEqual(topics[1]["views"], 42)
        self.assertEqual(topics[1]["author"], "alice")

        topics, filtered, _, _ = category_topics_from(payload, 10, 2)
        self.assertEqual([t["topic_id"] for t in topics], [3])
        self.assertEqual(filtered, 2)

        topics, _, _, truncated = category_topics_from(payload, 1, 0)
        self.assertEqual(len(topics), 1)
        self.assertTrue(truncated)


class DecodeTests(unittest.TestCase):
    def test_signed_out_and_rate_limits(self):
        with self.assertRaises(ForumError) as caught:
            decode(response('{"error_type":"not_logged_in"}', 403), "主题")
        self.assertEqual(caught.exception.code, "signed_out")

        with self.assertRaises(ForumError) as caught:
            decode(response("", 429), "主题")
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertIsNotNone(caught.exception.retry_at)

        with self.assertRaises(ForumError) as caught:
            decode(response("<html>", 200), "主题")
        self.assertEqual(caught.exception.code, "page_unrecognized")


if __name__ == "__main__":
    unittest.main()
