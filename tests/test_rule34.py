from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from sefbot import rule34


class Rule34ValidationTests(unittest.TestCase):
    def test_normalizes_character_label_and_rejects_query_operators(self) -> None:
        self.assertEqual(rule34.normalize_character("Kit Gameoverse"), "kit_gameoverse")
        for value in ("", "tag sort:score", "tag*", "-tag", "tag/name"):
            with self.subTest(value=value), self.assertRaises(rule34.Rule34Error):
                rule34.normalize_character(value)

    def test_plain_name_series_tag_gets_canonical_parenthesized_fallback(self) -> None:
        self.assertEqual(
            rule34._character_candidates("kit_gameoverse"),
            ("kit_gameoverse", "kit_(gameoverse)"),
        )
        self.assertEqual(
            rule34._character_candidates("kit_(gameoverse)"),
            ("kit_(gameoverse)",),
        )

    def test_rejects_underage_related_search_terms(self) -> None:
        for value in ("loli", "some_child", "underage_character", "young_person"):
            with self.subTest(value=value), self.assertRaises(rule34.Rule34Error):
                rule34.normalize_character(value)

    def test_amount_is_bounded_to_discords_ten_embed_limit(self) -> None:
        self.assertEqual(rule34.validate_amount(1), 1)
        self.assertEqual(rule34.validate_amount(10), 10)
        for value in (0, 11, "many"):
            with self.subTest(value=value), self.assertRaises(rule34.Rule34Error):
                rule34.validate_amount(value)

    def test_parse_posts_accepts_only_rule34_image_urls_and_safe_tags(self) -> None:
        payload = [
            {
                "id": 1,
                "tags": "kit_gameoverse solo",
                "sample_url": "https://api-cdn.rule34.xxx/images/a/sample.jpg",
                "file_url": "https://api-cdn.rule34.xxx/images/a/full.png",
            },
            {
                "id": 2,
                "tags": "kit_gameoverse underage",
                "sample_url": "https://api-cdn.rule34.xxx/images/b/sample.jpg",
            },
            {
                "id": 3,
                "tags": "kit_gameoverse",
                "sample_url": "https://attacker.example/image.jpg",
            },
            {
                "id": 4,
                "tags": "kit_gameoverse",
                "file_url": "https://api-cdn.rule34.xxx/images/d/video.mp4",
            },
        ]

        self.assertEqual(
            rule34.parse_posts(payload, 10),
            [
                rule34.Post(
                    post_id=1,
                    image_url="https://api-cdn.rule34.xxx/images/a/sample.jpg",
                    page_url="https://rule34.xxx/index.php?page=post&s=view&id=1",
                )
            ],
        )

    def test_thread_inherits_age_restriction_from_parent(self) -> None:
        safe = SimpleNamespace(is_nsfw=lambda: False, parent=None)
        parent = SimpleNamespace(is_nsfw=lambda: True, parent=None)
        thread = SimpleNamespace(is_nsfw=lambda: False, parent=parent)
        self.assertFalse(rule34.is_age_restricted_channel(safe))
        self.assertTrue(rule34.is_age_restricted_channel(thread))


class Rule34ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_stays_disabled_without_both_credentials(self) -> None:
        with (
            mock.patch.object(rule34.config, "RULE34_USER_ID", ""),
            mock.patch.object(rule34.config, "RULE34_API_KEY", ""),
            self.assertRaises(rule34.Rule34Error) as raised,
        ):
            await rule34.search("kit_gameoverse", 1)
        self.assertIn("not configured", str(raised.exception))

    async def test_search_retries_an_invalid_json_batch(self) -> None:
        valid_payload = (
            b'[{"id": 1, "tags": "kit_gameoverse", '
            b'"sample_url": "https://api-cdn.rule34.xxx/images/a/sample.jpg"}]'
        )

        class Content:
            def __init__(self, body: bytes) -> None:
                self.body = body

            async def read(self, _limit: int) -> bytes:
                return self.body

        class Response:
            status = 200

            def __init__(self, body: bytes) -> None:
                self.content = Content(body)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return None

        class Session:
            def __init__(self) -> None:
                self.responses = iter((b"invalid-json", valid_payload))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return None

            def get(self, *_args, **_kwargs):
                return Response(next(self.responses))

        session = Session()
        with (
            mock.patch.object(rule34.config, "RULE34_USER_ID", "1"),
            mock.patch.object(rule34.config, "RULE34_API_KEY", "key"),
            mock.patch.object(rule34.aiohttp, "ClientSession", return_value=session),
            mock.patch.object(rule34.asyncio, "sleep", mock.AsyncMock()),
        ):
            tag, posts = await rule34.search("kit_gameoverse", 1)
        self.assertEqual(tag, "kit_gameoverse")
        self.assertEqual(len(posts), 1)


if __name__ == "__main__":
    unittest.main()
