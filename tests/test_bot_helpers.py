from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from io import BytesIO

from PIL import Image

from bot import (
    BANNER_SIZE,
    DISCORD_MESSAGE_LIMIT,
    HOST_DEFAULT_USAGE,
    PERSONA_USAGE,
    MessageEventGuard,
    age_restricted_channel,
    command_text,
    image_url,
    is_owner_note_command,
    matched_command,
    language_avatar_path,
    language_banner_path,
    looks_like_image,
    parse_language_name,
    parse_persona_argument,
    prepare_avatar_bytes,
    prepare_banner_bytes,
    referenced_author_id,
    referenced_message_id,
    split_reply,
)
from music import MUSIC_USAGE, music_error_reply


class BotHelperTests(unittest.TestCase):
    def test_message_event_guard_claims_each_event_once(self) -> None:
        guard = MessageEventGuard(ttl=10)

        self.assertTrue(guard.claim(42, now=100))
        self.assertFalse(guard.claim(42, now=101))
        self.assertTrue(guard.claim(42, now=111))

    def test_owner_note_command_matches_straight_and_curly_apostrophes(self) -> None:
        self.assertTrue(is_owner_note_command("!owner's note"))
        self.assertTrue(is_owner_note_command("  !OWNER’S NOTE  "))
        self.assertFalse(is_owner_note_command("!owner's note please"))
        self.assertFalse(is_owner_note_command("!help"))

    def test_matched_command_recognizes_prefix_commands(self) -> None:
        self.assertEqual(matched_command("!help"), "!help")
        self.assertEqual(matched_command("!HELP"), "!help")
        self.assertEqual(matched_command("!persona nerdish"), "!persona")
        self.assertEqual(matched_command("!debate pineapple on pizza"), "!debate")
        self.assertEqual(matched_command("!music skip"), "!music")
        self.assertEqual(matched_command("!owner's note"), "!owner's note")
        self.assertEqual(matched_command("!OWNER’S NOTE"), "!owner's note")
        self.assertEqual(matched_command("!persona host default gpt"), "!persona")
        self.assertIsNone(matched_command("hello"))
        self.assertIsNone(matched_command("!unknown"))
        self.assertIsNone(matched_command("!owner's"))

    def test_parse_persona_argument_accepts_host_default_models(self) -> None:
        self.assertEqual(parse_persona_argument("rudeish"), ("rudeish", None))
        self.assertEqual(parse_persona_argument("host default"), ("host-default-gpt", None))
        self.assertEqual(
            parse_persona_argument("host default GPT"), ("host-default-gpt", None)
        )
        self.assertEqual(
            parse_persona_argument("host-default deepseek"),
            ("host-default-deepseek", None),
        )
        self.assertEqual(
            parse_persona_argument("host default mistral"),
            ("host-default-mistral", None),
        )
        persona, error = parse_persona_argument("host default claude")
        self.assertIsNone(persona)
        self.assertEqual(error, HOST_DEFAULT_USAGE)
        persona, error = parse_persona_argument("mystery")
        self.assertIsNone(persona)
        self.assertEqual(error, PERSONA_USAGE)

    def test_command_text_strips_bot_mentions_so_prefix_commands_still_match(self) -> None:
        self.assertEqual(command_text("!persona nerdish"), "!persona nerdish")
        self.assertEqual(
            command_text("<@99> !persona nerdish", 99), "!persona nerdish"
        )
        self.assertEqual(
            command_text("!persona nerdish <@!99>", 99), "!persona nerdish"
        )
        self.assertEqual(command_text("！persona nerdish", 99), "!persona nerdish")
        self.assertEqual(
            command_text("<@99> !language hebrew", 99), "!language hebrew"
        )

    def test_age_restricted_channel_uses_discord_nsfw_flag(self) -> None:
        self.assertFalse(age_restricted_channel(SimpleNamespace()))
        self.assertFalse(age_restricted_channel(SimpleNamespace(nsfw=False)))
        self.assertTrue(age_restricted_channel(SimpleNamespace(nsfw=True)))

    def test_image_url_only_accepts_image_attachments(self) -> None:
        image = SimpleNamespace(
            content_type="image/png", url="https://cdn.discordapp.com/cat.png"
        )
        other = SimpleNamespace(
            content_type="application/pdf", url="https://cdn.discordapp.com/file.pdf"
        )
        self.assertEqual(image_url(image), "https://cdn.discordapp.com/cat.png")
        self.assertIsNone(image_url(other))

    def test_members_only_music_errors_are_sanitized(self) -> None:
        error = RuntimeError(
            "This video is available to this channel's members on level: My Baby"
        )
        reply = music_error_reply("play that", error)
        self.assertIn("members-only", reply)
        self.assertNotIn("My Baby", reply)

    def test_language_command_requires_a_full_language_name(self) -> None:
        language, error = parse_language_name("hungarian")
        self.assertEqual(language, "hungarian")
        self.assertIsNone(error)

        language, error = parse_language_name("hu")
        self.assertIsNone(language)
        self.assertIn("full language name", error or "")

    def test_language_avatar_uses_the_matching_country_picture(self) -> None:
        hungarian = language_avatar_path("Hungarian")
        self.assertIsNotNone(hungarian)
        assert hungarian is not None
        self.assertEqual(hungarian.name, "hungarian.png")
        self.assertEqual(hungarian.parent.name, "pfps")

        self.assertIsNone(language_avatar_path("english"))
        self.assertIsNone(language_avatar_path("hebrew"))

    def test_language_banner_uses_the_matching_country_picture(self) -> None:
        hungarian = language_banner_path("Hungarian")
        self.assertIsNotNone(hungarian)
        assert hungarian is not None
        self.assertEqual(hungarian.name, "hungary.jpg")

        french = language_banner_path("french")
        self.assertIsNotNone(french)
        assert french is not None
        self.assertEqual(french.name, "france.jpg")

        self.assertIsNone(language_banner_path("english"))
        self.assertIsNone(language_banner_path("hebrew"))
        self.assertIsNone(language_banner_path("greek"))

    def test_language_avatar_can_load_pictures_from_an_avatars_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            themed = root / "avatars"
            themed.mkdir()
            picture = themed / "french.png"
            picture.write_bytes(b"fake-png")

            self.assertEqual(language_avatar_path("French", root=root), picture)
            self.assertIsNone(language_avatar_path("german", root=root))

    def test_language_banner_can_load_pictures_from_a_banners_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            themed = root / "banners"
            themed.mkdir()
            picture = themed / "hungary.jpg"
            picture.write_bytes(b"fake-jpg")

            self.assertEqual(language_banner_path("hungarian", root=root), picture)
            self.assertIsNone(language_banner_path("italian", root=root))

    def test_prepare_profile_images_makes_discord_safe_jpegs(self) -> None:
        from bot import ROOT

        avatar = prepare_avatar_bytes((ROOT / "pfps" / "hungarian.png").read_bytes())
        banner = prepare_banner_bytes((ROOT / "banners" / "romania.jpg").read_bytes())
        self.assertIsNotNone(avatar)
        self.assertIsNotNone(banner)
        assert avatar is not None
        assert banner is not None
        self.assertTrue(looks_like_image(avatar))
        self.assertTrue(looks_like_image(banner))
        self.assertEqual(Image.open(BytesIO(banner)).size, BANNER_SIZE)
        self.assertEqual(Image.open(BytesIO(avatar)).size, (1024, 1024))
        self.assertIsNone(prepare_avatar_bytes(b"not-an-image"))
        self.assertIsNone(prepare_banner_bytes(b"not-an-image"))
        self.assertFalse(looks_like_image(b"hello"))

    def test_music_usage_includes_restart(self) -> None:
        self.assertIn("!music restart", MUSIC_USAGE)
        self.assertIn("!music pause", MUSIC_USAGE)

    def test_referenced_message_helpers_read_discord_replies(self) -> None:
        self.assertIsNone(referenced_message_id(SimpleNamespace()))
        self.assertIsNone(referenced_author_id(SimpleNamespace()))
        reply = SimpleNamespace(
            reference=SimpleNamespace(
                message_id=7,
                resolved=SimpleNamespace(author=SimpleNamespace(id=99)),
            )
        )
        self.assertEqual(referenced_message_id(reply), 7)
        self.assertEqual(referenced_author_id(reply), 99)

    def test_split_reply_keeps_short_text_and_breaks_long_text(self) -> None:
        self.assertEqual(split_reply("hello"), ["hello"])
        self.assertEqual(split_reply("   "), [])
        long = "a" * (DISCORD_MESSAGE_LIMIT + 50)
        chunks = split_reply(long)
        self.assertEqual(len(chunks), 2)
        self.assertEqual("".join(chunks), long)
        self.assertTrue(all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in chunks))
        paragraph = ("word " * 400).strip()
        broken = split_reply(paragraph, limit=80)
        self.assertGreater(len(broken), 1)
        self.assertTrue(all(len(chunk) <= 80 for chunk in broken))
        self.assertEqual(" ".join(broken), paragraph)


if __name__ == "__main__":
    unittest.main()
