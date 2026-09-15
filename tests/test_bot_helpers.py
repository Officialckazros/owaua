from __future__ import annotations

import re
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from io import BytesIO
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from bot import (
    BANNER_SIZE,
    DISCORD_MESSAGE_LIMIT,
    FULL_MODE_ALLOWED_USER_IDS,
    FULL_MODE_CHANNEL_ID,
    FULL_MODE_GUILD_ID,
    HOST_DEFAULT_USAGE,
    PERSONA_USAGE,
    MessageEventGuard,
    age_restricted_channel,
    command_text,
    full_mode_allowed,
    full_mode_blocked,
    full_mode_can_enable,
    full_mode_location,
    image_url,
    is_full_mode_command,
    is_owner_note_command,
    matched_command,
    referenced_message_context,
    language_avatar_path,
    language_banner_path,
    looks_like_image,
    parse_language_name,
    parse_persona_argument,
    prepare_avatar_bytes,
    prepare_banner_bytes,
    split_reply,
)
from music import (
    LIVE_STREAM_REPLY,
    LONG_TRACK_REPLY,
    MUSIC_USAGE,
    NON_YOUTUBE_URL_REPLY,
    PLAYLIST_URL_REPLY,
    YTDLP_EXTRACTORS,
    _connect_to_author,
    abandon_music_if_needed,
    ffmpeg_before_options,
    music_error_reply,
    music_lookup,
    opus_codec,
    play_track,
    resolve_music,
    safe_http_url,
)


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
        self.assertIsNone(matched_command("!active on"))
        self.assertIsNone(matched_command("!debate pineapple on pizza"))
        self.assertEqual(matched_command("!music skip"), "!music")
        self.assertEqual(matched_command("!owner's note"), "!owner's note")
        self.assertEqual(matched_command("!OWNER’S NOTE"), "!owner's note")
        self.assertEqual(matched_command("!persona host default gpt"), "!persona")
        self.assertIsNone(matched_command("!full mode on"))
        self.assertIsNone(matched_command("hello"))
        self.assertIsNone(matched_command("!unknown"))
        self.assertIsNone(matched_command("!owner's"))

    def test_full_mode_command_only_matches_the_full_prefix(self) -> None:
        self.assertTrue(is_full_mode_command("!full mode on"))
        self.assertTrue(is_full_mode_command("!FULL MODE OFF"))
        self.assertTrue(is_full_mode_command("!full"))
        self.assertFalse(is_full_mode_command("!fully"))
        self.assertFalse(is_full_mode_command("!help"))

    def test_full_mode_location_is_one_guild_and_channel(self) -> None:
        allowed = SimpleNamespace(
            guild=SimpleNamespace(id=FULL_MODE_GUILD_ID),
            channel=SimpleNamespace(id=FULL_MODE_CHANNEL_ID),
        )
        other_channel = SimpleNamespace(
            guild=SimpleNamespace(id=FULL_MODE_GUILD_ID),
            channel=SimpleNamespace(id=22),
        )
        other_guild = SimpleNamespace(
            guild=SimpleNamespace(id=11),
            channel=SimpleNamespace(id=FULL_MODE_CHANNEL_ID),
        )
        dm = SimpleNamespace(guild=None, channel=SimpleNamespace(id=FULL_MODE_CHANNEL_ID))
        self.assertTrue(full_mode_location(allowed))
        self.assertFalse(full_mode_location(other_channel))
        self.assertFalse(full_mode_location(other_guild))
        self.assertFalse(full_mode_location(dm))
        self.assertTrue(full_mode_blocked(470617205667790868))
        self.assertFalse(full_mode_blocked(33))
        self.assertTrue(full_mode_can_enable(1172433512364769342))
        self.assertTrue(full_mode_can_enable(836988339491962881))
        self.assertFalse(full_mode_can_enable(33))
        self.assertFalse(full_mode_can_enable(470617205667790868))
        self.assertTrue(full_mode_allowed(836988339491962881))
        self.assertTrue(full_mode_allowed(1391094791210536970))
        self.assertTrue(full_mode_allowed(next(iter(FULL_MODE_ALLOWED_USER_IDS))))
        self.assertTrue(full_mode_allowed(1172433512364769342))
        self.assertFalse(full_mode_allowed(33))
        self.assertFalse(full_mode_allowed(470617205667790868))

    def test_referenced_message_context_quotes_the_replied_to_text(self) -> None:
        bot_reply = SimpleNamespace(
            content="Charlie Kirk died on September 10, 2025. He was 31.",
            author=SimpleNamespace(id=99),
        )
        other = SimpleNamespace(
            content="<@99> look at this",
            author=SimpleNamespace(id=44),
        )
        self.assertEqual(
            referenced_message_context(
                SimpleNamespace(reference=SimpleNamespace(resolved=bot_reply)),
                99,
            ),
            "(replying to you: Charlie Kirk died on September 10, 2025. He was 31.)",
        )
        self.assertEqual(
            referenced_message_context(
                SimpleNamespace(reference=SimpleNamespace(resolved=other)),
                99,
            ),
            "(replying to someone: look at this)",
        )
        self.assertEqual(
            referenced_message_context(SimpleNamespace(reference=None), 99),
            "",
        )

    def test_parse_persona_argument_accepts_host_default_models(self) -> None:
        self.assertEqual(parse_persona_argument("rudeish"), ("rudeish", None))
        self.assertEqual(
            parse_persona_argument("host default"), ("host-default-deepseek", None)
        )
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

    def test_music_validation_errors_keep_their_message(self) -> None:
        self.assertEqual(
            music_error_reply("play that", ValueError(NON_YOUTUBE_URL_REPLY)),
            NON_YOUTUBE_URL_REPLY,
        )

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

    def test_ffmpeg_before_options_buffer_and_headers(self) -> None:
        plain = ffmpeg_before_options()
        self.assertIn("-thread_queue_size 1024", plain)
        self.assertIn("-reconnect 1", plain)
        self.assertIn("-nostdin", plain)
        self.assertIn("-protocol_whitelist", plain)
        self.assertNotIn("-headers", plain)

        with_headers = ffmpeg_before_options({"User-Agent": "yt-dlp"})
        self.assertIn("-headers", with_headers)
        self.assertIn("User-Agent", with_headers)
        self.assertIn("yt-dlp", with_headers)

        sneaky = ffmpeg_before_options({"X": "a\r\n -i http://evil.test/song.mp3"})
        self.assertNotIn("evil.test", sneaky)
        self.assertNotIn("-i http", sneaky)

    def test_music_lookup_keeps_a_single_youtube_video(self) -> None:
        self.assertEqual(
            music_lookup("radiohead creep"),
            "ytsearch1:radiohead creep",
        )
        self.assertEqual(
            music_lookup(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ&start_radio=1"
            ),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertEqual(
            music_lookup("https://youtu.be/dQw4w9WgXcQ?list=RDdQw4w9WgXcQ"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertEqual(
            music_lookup("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVM"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertEqual(
            music_lookup("https://www.youtube.com/shorts/dQw4w9WgXcQ"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    def test_music_lookup_rejects_playlists_and_non_youtube_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, re.escape(NON_YOUTUBE_URL_REPLY)):
            music_lookup("https://evil.example/playlist.m3u")
        with self.assertRaisesRegex(ValueError, re.escape(NON_YOUTUBE_URL_REPLY)):
            music_lookup("concat:http://a.test/a.mp3|http://b.test/b.mp3")
        with self.assertRaisesRegex(ValueError, re.escape(NON_YOUTUBE_URL_REPLY)):
            music_lookup("file:///etc/passwd")
        with self.assertRaisesRegex(ValueError, re.escape(PLAYLIST_URL_REPLY)):
            music_lookup("https://www.youtube.com/playlist?list=PLabcdefghij")
        with self.assertRaisesRegex(ValueError, re.escape(PLAYLIST_URL_REPLY)):
            music_lookup("https://www.youtube.com/watch?list=RDdQw4w9WgXcQ")

    def test_safe_http_url_rejects_ffmpeg_and_ssrf_tricks(self) -> None:
        self.assertTrue(safe_http_url("https://example.test/audio"))
        self.assertFalse(safe_http_url("concat:http://a.test/a|http://b.test/b"))
        self.assertFalse(safe_http_url("file:///etc/passwd"))
        self.assertFalse(safe_http_url("https://127.0.0.1/audio.mp3"))
        self.assertFalse(safe_http_url("http://169.254.169.254/latest/meta-data/"))
        self.assertFalse(safe_http_url("http://localhost/audio.mp3"))
        self.assertFalse(safe_http_url("https://example.test/a\n-i http://evil.test"))

    def test_opus_codec_copies_opus_only(self) -> None:
        self.assertEqual(opus_codec("opus"), "copy")
        self.assertEqual(opus_codec("opus.webm"), "copy")
        self.assertIsNone(opus_codec("aac"))
        self.assertIsNone(opus_codec(""))

    def test_play_track_uses_bounded_downloaded_audio(self) -> None:
        voice = SimpleNamespace(play=Mock())
        source = Mock()
        with patch("music.BoundedAudio", return_value=source) as audio:
            play_track(voice, {"audio_bytes": b"OggSdata"})
        audio.assert_called_once_with(b"OggSdata")
        voice.play.assert_called_once()

    def test_play_track_rejects_non_http_stream_urls(self) -> None:
        voice = SimpleNamespace(play=Mock())
        with self.assertRaises(ValueError):
            play_track(
                voice,
                {
                    "url": "concat:http://a.test/a.mp3|http://b.test/b.mp3",
                    "acodec": "",
                    "http_headers": {},
                },
            )
        voice.play.assert_not_called()

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


class MusicAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_music_uses_disposable_worker(self) -> None:
        info = {"title": "Creep", "url": "https://r1.googlevideo.com/audio", "duration": 200, "acodec": "opus"}
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(info).encode(), b"")), wait=AsyncMock())
        with patch("music.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            track = await resolve_music("radiohead creep")
        self.assertEqual(spawn.call_args.args[-1], "ytsearch1:radiohead creep")
        self.assertNotIn("OPENAI_API_KEY", spawn.call_args.kwargs["env"])
        self.assertEqual(track["title"], "Creep")
        self.assertEqual(track["http_headers"], {})

    async def test_resolve_music_strips_mix_parameters_before_ytdlp(self) -> None:
        info = {"title": "Song", "url": "https://r1.googlevideo.com/audio", "duration": 200}
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(info).encode(), b"")), wait=AsyncMock())
        with patch("music.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            await resolve_music("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ")
        self.assertEqual(spawn.call_args.args[-1], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    async def test_resolve_music_does_not_fetch_non_youtube_urls(self) -> None:
        class FakeYoutubeDL:
            called = False

            def __init__(self, options: dict[str, object]) -> None:
                pass

            def __enter__(self) -> FakeYoutubeDL:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def extract_info(
                self, lookup: str, download: bool = False
            ) -> dict[str, object]:
                type(self).called = True
                raise AssertionError("yt-dlp should not run for non-YouTube URLs")

        fake_module = types.ModuleType("yt_dlp")
        fake_module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"yt_dlp": fake_module}):
            with self.assertRaisesRegex(ValueError, re.escape(NON_YOUTUBE_URL_REPLY)):
                await resolve_music("https://evil.example/playlist.m3u")
        self.assertFalse(FakeYoutubeDL.called)

    async def test_resolve_music_rejects_live_radios(self) -> None:
        info = {"url": "https://r1.googlevideo.com/audio", "is_live": True}
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(info).encode(), b"")), wait=AsyncMock())
        with patch("music.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaisesRegex(ValueError, re.escape(LIVE_STREAM_REPLY)):
                await resolve_music("song")

    async def test_resolve_music_rejects_long_mixes(self) -> None:
        info = {"url": "https://r1.googlevideo.com/audio", "duration": 10800}
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(info).encode(), b"")), wait=AsyncMock())
        with patch("music.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaisesRegex(ValueError, re.escape(LONG_TRACK_REPLY)):
                await resolve_music("song")

    async def test_music_stops_when_the_requester_leaves(self) -> None:
        voice = SimpleNamespace(
            channel=SimpleNamespace(id=7, members=[]),
            stop=Mock(),
            disconnect=AsyncMock(),
        )
        guild = SimpleNamespace(id=11, voice_client=voice)
        voice.channel.members = []
        bot = SimpleNamespace(
            music_tracks={11: {"title": "x", "requested_by": 33}},
        )
        member = SimpleNamespace(id=33, bot=False, guild=guild)
        stopped = await abandon_music_if_needed(
            bot,
            member,
            SimpleNamespace(channel=voice.channel),
            SimpleNamespace(channel=None),
        )
        self.assertTrue(stopped)
        voice.stop.assert_called_once()
        voice.disconnect.assert_awaited_once()
        self.assertEqual(bot.music_tracks, {})

    async def test_music_keeps_playing_when_someone_else_leaves(self) -> None:
        requester = SimpleNamespace(id=33, bot=False)
        voice = SimpleNamespace(
            channel=SimpleNamespace(id=7, members=[requester]),
            stop=Mock(),
            disconnect=AsyncMock(),
        )
        guild = SimpleNamespace(id=11, voice_client=voice)
        bot = SimpleNamespace(
            music_tracks={11: {"title": "x", "requested_by": 33}},
        )
        bystander = SimpleNamespace(id=44, bot=False, guild=guild)
        stopped = await abandon_music_if_needed(
            bot,
            bystander,
            SimpleNamespace(channel=voice.channel),
            SimpleNamespace(channel=None),
        )
        self.assertFalse(stopped)
        voice.stop.assert_not_called()
        voice.disconnect.assert_not_awaited()
        self.assertIn(11, bot.music_tracks)

    async def test_connect_rejects_a_voice_channel_from_another_server(self) -> None:
        other = SimpleNamespace(id=99)
        channel = SimpleNamespace(id=7, guild=other, connect=AsyncMock())
        guild = SimpleNamespace(id=11, voice_client=None, me=None)
        message = SimpleNamespace(
            author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
            guild=guild,
        )

        client, error = await _connect_to_author(message, None)

        self.assertIsNone(client)
        self.assertIn("this server", error or "")
        channel.connect.assert_not_called()

    async def test_connect_self_deafens(self) -> None:
        voice = SimpleNamespace(channel=SimpleNamespace(id=7), guild=None)
        channel = SimpleNamespace(
            id=7,
            guild=SimpleNamespace(),
            connect=AsyncMock(return_value=voice),
        )
        guild = SimpleNamespace(voice_client=None, me=None)
        message = SimpleNamespace(
            author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
            guild=guild,
        )

        client, error = await _connect_to_author(message, None)

        self.assertIs(client, voice)
        self.assertIsNone(error)
        channel.connect.assert_awaited_once_with(self_deaf=True)


if __name__ == "__main__":
    unittest.main()
