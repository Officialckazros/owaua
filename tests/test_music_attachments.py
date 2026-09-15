from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from music import NON_YOUTUBE_URL_REPLY, attachment_track, handle_music_command


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

    async def test_music_command_streams_attachment_without_ytdlp(self) -> None:
        attachment = SimpleNamespace(
            size=100,
            filename="song.flac",
            content_type="audio/flac",
            url="https://cdn.discordapp.com/song.flac",
        )
        message, voice = make_music_message(attachment)
        bot = SimpleNamespace(music_tracks={}, user=None)

        with (
            patch("music.resolve_music", AsyncMock()) as resolve,
            patch("music.download_audio", AsyncMock(return_value=b"OggSfake")),
            patch("music.play_track") as play,
        ):
            reply = await handle_music_command(bot, message, "")

        resolve.assert_not_awaited()
        play.assert_called_once()
        self.assertEqual(play.call_args.args[1]["audio_bytes"], b"OggSfake")
        self.assertEqual(reply, "playing: song.flac")
        self.assertEqual(
            bot.music_tracks[11]["url"],
            "https://cdn.discordapp.com/song.flac",
        )

    async def test_restart_reuses_attachment_without_ytdlp(self) -> None:
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
            patch("music.resolve_music", AsyncMock()) as resolve,
            patch("music.download_audio", AsyncMock(return_value=b"OggSfake")),
            patch("music.play_track") as play,
        ):
            reply = await handle_music_command(bot, message, "restart")

        resolve.assert_not_awaited()
        play.assert_called_once()
        played = play.call_args.args[1]
        self.assertIs(play.call_args.args[0], voice)
        self.assertEqual(played["url"], track["url"])
        self.assertEqual(played["requested_by"], 33)
        self.assertEqual(reply, "restarted: song.ogg")

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


if __name__ == "__main__":
    unittest.main()
