from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from music import (
    NON_YOUTUBE_URL_REPLY,
    UNRESTRICTED_MUSIC_GUILD_IDS,
    attachment_track,
    handle_music_command,
    unrestricted_mp3_track,
)


def make_music_message(attachment: object) -> tuple[SimpleNamespace, SimpleNamespace]:
    voice = SimpleNamespace(
        channel=SimpleNamespace(id=7),
        guild=None,
        is_playing=lambda: False,
        is_paused=lambda: False,
        stop=Mock(),
    )
    guild = SimpleNamespace(id=11, voice_client=voice)
    target_channel = SimpleNamespace(id=7, guild=guild)
    message = SimpleNamespace(
        guild=guild,
        channel=SimpleNamespace(id=22),
        author=SimpleNamespace(
            id=33,
            voice=SimpleNamespace(channel=target_channel),
        ),
        attachments=[attachment],
    )
    return message, voice


class MusicAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def test_mp3_attachment_and_direct_link_are_recognized(self) -> None:
        attachment = SimpleNamespace(
            size=100,
            filename="song.mp3",
            content_type="audio/mpeg",
            url="https://cdn.discordapp.com/song.mp3?signature=abc",
        )
        attached = attachment_track(attachment)
        direct = unrestricted_mp3_track(
            "https://media.example.test/music/song.mp3?token=abc"
        )

        self.assertIsNotNone(attached)
        self.assertEqual(attached["title"], "song.mp3")
        self.assertIsNotNone(direct)
        self.assertEqual(direct["title"], "song.mp3")
        self.assertEqual(
            direct["url"],
            "https://media.example.test/music/song.mp3?token=abc",
        )

    def test_attachment_track_accepts_audio_mime_types(self) -> None:
        attachment = SimpleNamespace(
            size=100,
            filename="recording.ogg",
            content_type="audio/x-custom; charset=binary",
            url="https://cdn.discordapp.com/recording.ogg",
        )

        track = attachment_track(attachment)

        self.assertIsNotNone(track)
        assert track is not None
        self.assertEqual(track["title"], "recording.ogg")
        self.assertEqual(track["source"], "attachment")

    def test_attachment_track_rejects_tracker_formats(self) -> None:
        attachment = SimpleNamespace(
            size=100,
            filename="module.xm",
            content_type="application/octet-stream",
            url="https://cdn.discordapp.com/module.xm",
        )

        self.assertIsNone(attachment_track(attachment))

    def test_attachment_track_accepts_audio_in_a_webm_container(self) -> None:
        attachment = SimpleNamespace(
            size=100,
            filename="recording.webm",
            content_type="video/webm",
            url="https://cdn.discordapp.com/recording.webm",
        )

        self.assertIsNotNone(attachment_track(attachment))

    def test_attachment_track_rejects_a_known_non_audio_file(self) -> None:
        attachment = SimpleNamespace(
            size=100,
            filename="notes.pdf",
            content_type="application/pdf",
            url="https://cdn.discordapp.com/notes.pdf",
        )

        self.assertIsNone(attachment_track(attachment))

    def test_attachment_track_rejects_playlist_files(self) -> None:
        playlist = SimpleNamespace(
            size=100,
            filename="radio.m3u",
            content_type="audio/x-mpegurl",
            url="https://cdn.discordapp.com/radio.m3u",
        )
        untitled = SimpleNamespace(
            size=100,
            filename="stream",
            content_type="application/vnd.apple.mpegurl",
            url="https://cdn.discordapp.com/stream",
        )
        self.assertIsNone(attachment_track(playlist))
        self.assertIsNone(attachment_track(untitled))

    def test_attachment_track_rejects_non_discord_urls(self) -> None:
        attachment = SimpleNamespace(
            size=100,
            filename="song.mp3",
            content_type="audio/mpeg",
            url="https://evil.example/song.mp3",
        )
        self.assertIsNone(attachment_track(attachment))

    def test_attachment_track_rejects_generic_files_without_audio_extension(
        self,
    ) -> None:
        attachment = SimpleNamespace(
            size=100,
            filename="payload.bin",
            content_type="application/octet-stream",
            url="https://cdn.discordapp.com/payload.bin",
        )
        self.assertIsNone(attachment_track(attachment))

    async def test_music_command_rejects_attachments(self) -> None:
        attachment = SimpleNamespace(
            size=100,
            filename="song.flac",
            content_type="audio/flac",
            url="https://cdn.discordapp.com/song.flac",
        )
        message, voice = make_music_message(attachment)
        bot = SimpleNamespace(music_tracks={}, user=None)

        with (
            patch("music.resolve_music", AsyncMock(side_effect=ValueError(NON_YOUTUBE_URL_REPLY))) as resolve,
            patch("music.download_audio", AsyncMock(return_value=b"OggSfake")),
            patch("music.play_track") as play,
        ):
            reply = await handle_music_command(bot, message, "")

        resolve.assert_not_awaited()
        play.assert_not_called()
        self.assertEqual(reply, "usage: !music <YouTube video or Twitter/X post URL> | !music start | !music pause | !music resume | !music restart | !music stop | !music skip | !music leave | !music now")
        self.assertEqual(bot.music_tracks, {})

    async def test_restart_does_not_reuse_attachment_tracks(self) -> None:
        attachment = SimpleNamespace(
            size=100,
            filename="song.ogg",
            content_type="audio/ogg",
            url="https://cdn.discordapp.com/song.ogg",
        )
        message, voice = make_music_message(attachment)
        message.attachments = []
        track = attachment_track(attachment)
        assert track is not None
        bot = SimpleNamespace(music_tracks={11: track}, user=None)

        with (
            patch("music.resolve_music", AsyncMock(side_effect=ValueError(NON_YOUTUBE_URL_REPLY))) as resolve,
            patch("music.download_audio", AsyncMock(return_value=b"OggSfake")),
            patch("music.play_track") as play,
        ):
            reply = await handle_music_command(bot, message, "restart")

        resolve.assert_awaited_once_with(track["query"])
        play.assert_not_called()
        self.assertEqual(reply, NON_YOUTUBE_URL_REPLY)

    async def test_music_command_rejects_non_youtube_urls(self) -> None:
        attachment = SimpleNamespace(
            size=100,
            filename="song.mp3",
            content_type="audio/mpeg",
            url="https://cdn.discordapp.com/song.mp3",
        )
        message, voice = make_music_message(attachment)
        message.attachments = []
        bot = SimpleNamespace(music_tracks={}, user=None)

        with patch("music.play_track") as play:
            reply = await handle_music_command(
                bot, message, "https://evil.example/playlist.m3u"
            )

        play.assert_not_called()
        self.assertEqual(reply, NON_YOUTUBE_URL_REPLY)
        self.assertEqual(bot.music_tracks, {})

    async def test_trusted_guild_streams_any_resolved_link_without_limits(self) -> None:
        message, voice = make_music_message(SimpleNamespace())
        message.attachments = []
        message.guild.id = next(iter(UNRESTRICTED_MUSIC_GUILD_IDS))
        message.author.voice.channel.guild = message.guild
        bot = SimpleNamespace(
            music_tracks={1: {}, 2: {}}, music_busy=set(), user=None
        )
        track = {
            "title": "unrestricted",
            "url": "http://media.example.test/live.m3u8",
            "query": "http://media.example.test/watch",
            "source": "unrestricted",
            "http_headers": {},
        }

        with (
            patch("music.resolve_music", AsyncMock(return_value=track)) as resolve,
            patch("music.download_audio", AsyncMock()) as download,
            patch("music.play_track") as play,
        ):
            reply = await handle_music_command(
                bot, message, "http://media.example.test/watch"
            )

        resolve.assert_awaited_once_with(
            "http://media.example.test/watch", unrestricted=True
        )
        download.assert_not_awaited()
        self.assertTrue(play.call_args.kwargs["unrestricted"])
        self.assertEqual(reply, "playing: unrestricted")

    async def test_trusted_guild_streams_direct_mp3_without_resolver(self) -> None:
        message, _voice = make_music_message(SimpleNamespace())
        message.attachments = []
        message.guild.id = next(iter(UNRESTRICTED_MUSIC_GUILD_IDS))
        message.author.voice.channel.guild = message.guild
        bot = SimpleNamespace(music_tracks={}, music_busy=set(), user=None)
        url = "https://media.example.test/audio/song.mp3?signature=abc"

        with (
            patch("music.resolve_music", AsyncMock()) as resolve,
            patch("music.download_audio", AsyncMock()) as download,
            patch("music.play_track") as play,
        ):
            reply = await handle_music_command(bot, message, url)

        resolve.assert_not_awaited()
        download.assert_not_awaited()
        self.assertEqual(play.call_args.args[1]["url"], url)
        self.assertTrue(play.call_args.kwargs["unrestricted"])
        self.assertEqual(reply, "playing: song.mp3")

    async def test_trusted_guild_accepts_audio_attachments(self) -> None:
        attachment = SimpleNamespace(
            size=10**12,
            filename="unrestricted.mp3",
            content_type="audio/mpeg",
            url="http://media.example.test/unrestricted.mp3",
        )
        message, _voice = make_music_message(attachment)
        message.guild.id = next(iter(UNRESTRICTED_MUSIC_GUILD_IDS))
        message.author.voice.channel.guild = message.guild
        bot = SimpleNamespace(music_tracks={}, music_busy=set(), user=None)

        with (
            patch("music.resolve_music", AsyncMock()) as resolve,
            patch("music.download_audio", AsyncMock()) as download,
            patch("music.play_track") as play,
        ):
            reply = await handle_music_command(bot, message, "")

        resolve.assert_not_awaited()
        download.assert_not_awaited()
        self.assertTrue(play.call_args.kwargs["unrestricted"])
        self.assertEqual(reply, "playing: unrestricted.mp3")


if __name__ == "__main__":
    unittest.main()
