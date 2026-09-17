"""Security boundaries: all provider/network operations are mocked."""

import asyncio
import io
import json
import os
import sqlite3
import shutil
import sys
import subprocess
import tempfile
import time
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from ask import ask
from bot import MessageEventGuard
from memory import CONVERSATION_MESSAGES, MAX_STORED_CHARS, MemoryStore
from music import download_audio, handle_music_command, media_format, resolve_music
from security import ApiLimits, BudgetExceeded, DuplicateRequest
import test_channel_commands as fixtures

FakeChannel = fixtures.FakeChannel
make_message = fixtures.make_message


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.sqlite3"
        self.store = MemoryStore(self.path)
        self.limits = ApiLimits(100, 100, 100, 100, 3)

    def test_concurrent_instances_cannot_overspend(self):
        stores = [MemoryStore(self.path) for _ in range(8)]
        def attempt(index):
            try:
                stores[index].reserve_api_request(str(index), str(index), str(index), limits=self.limits)
                return True
            except BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(8))), 3)

    def test_restart_erase_and_rolling_retention_do_not_reset_lifetime(self):
        for i in range(3):
            self.store.reserve_api_request(str(i), "u", "g", limits=self.limits, now=100)
        self.store.erase_server_memory("g")
        self.store.erase_user_memory("u")
        with self.assertRaises(BudgetExceeded):
            MemoryStore(self.path).reserve_api_request("next", "other", "other", limits=self.limits, now=1000000)

    def test_each_budget_dimension_blocks_independently(self):
        for field in ("per_minute", "per_user_day", "per_guild_day", "per_day", "lifetime"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                store = MemoryStore(Path(folder)/"m.sqlite3")
                values = dict(per_minute=100, per_user_day=100, per_guild_day=100, per_day=100, lifetime=100)
                values[field] = 1
                limits = ApiLimits(**values)
                store.reserve_api_request("1", "u", "g", limits=limits, now=100)
                with self.assertRaises(BudgetExceeded):
                    store.reserve_api_request("2", "u", "g", limits=limits, now=101)

    def test_clock_rollback_does_not_reopen_window(self):
        limits = ApiLimits(1, 100, 100, 100, 100)
        self.store.reserve_api_request("1", "u", "g", limits=limits, now=100)
        with self.assertRaises(BudgetExceeded):
            self.store.reserve_api_request("2", "v", "h", limits=limits, now=1)
        self.store.reserve_api_request("3", "v", "h", limits=limits, now=161)

    def test_duplicates_and_pause_apply_only_to_standard_requests(self):
        self.store.reserve_api_request("1", "u", "g")
        with self.assertRaises(DuplicateRequest):
            self.store.reserve_api_request("1", "u", "g")
        self.store.set_setting("api_paused", "1")
        with self.assertRaises(BudgetExceeded):
            self.store.reserve_api_request("2", "u", "g")
        self.store.reserve_api_request("3", "u", "g", exempt=True)

    def test_exempt_full_mode_does_not_consume_shared_budget(self):
        limits = ApiLimits(100, 100, 100, 100, 1)
        self.store.reserve_api_request("full", "allow", "g", limits=limits, exempt=True)
        self.store.reserve_api_request("normal", "u", "g", limits=limits)
        with self.assertRaises(BudgetExceeded):
            self.store.reserve_api_request("next", "v", "h", limits=limits)
        self.store.reserve_api_request("still-full", "allow", "g", limits=limits, exempt=True)

    def test_unbounded_memory_keeps_full_mode_history_verbatim(self):
        content = "x" * (MAX_STORED_CHARS + 1)
        for index in range(CONVERSATION_MESSAGES + 1):
            self.store.append_message(
                event_id=f"full-{index}",
                scope_id="full", user_id="allow", role="user", content=content,
                unbounded=True,
            )

        rows = self.store.recent_messages("full", "allow", limit=None)

        self.assertEqual(len(rows), CONVERSATION_MESSAGES + 1)
        self.assertTrue(all(row["content"] == content for row in rows))

    def test_retention_and_user_erasure(self):
        for i in range(25):
            self.store.append_message(event_id=str(i), scope_id="c", user_id="u", role="user", content="x"*8000)
        rows = self.store.recent_messages("c", "u", limit=100)
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(len(row["content"]) <= 5700 for row in rows))
        self.store.append_message(event_id="old", scope_id="c", user_id="other", role="user", content="old", created_at=time.time()-8*86400)
        self.assertEqual(self.store.recent_messages("c", "other", limit=10), [])
        self.store.append_message(event_id="other", scope_id="c", user_id="other", role="user", content="keep")
        self.store.erase_user_memory("u")
        self.assertEqual(self.store.recent_messages("c", "u", limit=10), [])
        self.assertEqual(len(self.store.recent_messages("c", "other", limit=10)), 1)

    def test_startup_prunes_legacy_expired_and_oversized_data(self):
        self.store.append_message(event_id="old", scope_id="c", user_id="u", role="user", content="x")
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE messages SET created_at=?, content=?", (time.time()-8*86400, "x"*9000))
        reopened = MemoryStore(self.path)
        self.assertEqual(reopened.recent_messages("c", "u", limit=10), [])

    def test_erasure_fences_writes_from_old_generation(self):
        for erase in (lambda: self.store.erase_user_memory("u"), lambda: self.store.erase_server_memory("g")):
            generation = self.store.memory_generation("u", "g")
            erase()
            self.assertFalse(self.store.append_message(event_id=str(generation), scope_id="c", user_id="u", server_id="g", role="assistant", content="private", expected_generation=generation))
            with self.assertRaises(BudgetExceeded):
                self.store.reserve_api_request(str(generation), "u", "g", expected_generation=generation, server_id="g")
            with self.assertRaises(BudgetExceeded):
                self.store.reserve_api_request(
                    f"{generation}-full",
                    "u",
                    "g",
                    expected_generation=generation,
                    server_id="g",
                    exempt=True,
                )


class BotSecurityTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ChannelCommandTests.setUp
    tearDown = fixtures.ChannelCommandTests.tearDown
    async def test_ordinary_member_cannot_change_shared_settings(self):
        for i, command in enumerate(("!language hungarian", "!language reset")):
            self.bot.command_used.clear()
            message = make_message(command, 100+i, FakeChannel(), manage_guild=False)
            await self.bot.on_message(message)
            self.assertIn("Manage Server", message.channel.sent[0])
            message.guild.me.edit.assert_not_called()
        self.assertEqual(self.store.get_setting("persona:guild:11"), "")

    async def test_persona_is_per_user(self):
        channel = FakeChannel()
        with patch("bot.host_model_error", return_value=None):
            await self.bot.on_message(
                make_message(
                    "!persona nerdish",
                    100,
                    channel,
                    author_id=100,
                    manage_guild=False,
                )
            )
            self.bot.command_used.clear()
            await self.bot.on_message(make_message("!persona", 101, channel, author_id=101))

        self.assertEqual(channel.sent, ["persona: nerdish", "persona: rudeish"])
        self.assertEqual(self.bot.persona_for(channel, make_message("hi", 102, channel, author_id=100)), "nerdish")
        self.assertEqual(self.bot.persona_for(channel, make_message("hi", 103, channel, author_id=101)), "rudeish")

    async def test_persona_isolated_between_users(self):
        first = make_message("!persona nerdish", 100, FakeChannel(), guild_id=11)
        await self.bot.on_message(first)
        other = make_message("!persona", 101, FakeChannel(44), guild_id=12, author_id=44)
        await self.bot.on_message(other)
        self.assertEqual(other.channel.sent, ["persona: rudeish"])
        private = make_message("!persona flirty", 102, FakeChannel(55, nsfw=True), guild_id=None, author_id=55)
        with patch("bot.host_model_error", return_value=None):
            await self.bot.on_message(private)
        self.assertEqual(self.bot.persona_for(first.channel, first), "nerdish")

    async def test_dm_disabled_and_blocklist_reject_before_provider(self):
        with patch("bot.ALLOW_DMS", False), patch("bot.ask", AsyncMock()) as provider:
            await self.bot.on_message(make_message("hi", 100, FakeChannel(), guild_id=None))
            provider.assert_not_awaited()

    async def test_dm_erasure_remains_available_when_chat_is_disabled(self):
        self.store.append_message(event_id="old", scope_id="22", user_id="33", role="user", content="private")
        with patch("bot.ALLOW_DMS", False):
            message = make_message("!memory erase mine", 100, FakeChannel(), guild_id=None)
            await self.bot.on_message(message)
        self.assertIn("erased", message.channel.sent[0])
        self.assertEqual(self.store.recent_messages("22", "33", limit=10), [])
        with patch("bot.BLOCKED_USERS", {33}), patch("bot.ask", AsyncMock()) as provider:
            await self.bot.on_message(make_message("hi", 101, FakeChannel(), mentions=[self.bot.user]))
            provider.assert_awaited_once()
            self.assertEqual(provider.await_args.kwargs["persona"], "blocked")
            self.assertEqual(provider.await_args.kwargs["provider_override"], "groq")
            self.assertFalse(provider.await_args.kwargs["full_mode"])

    async def test_busy_user_cannot_queue_across_channels_and_cancellation_releases(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            started.set()
            await finish.wait()
            return "hello"
        with patch("bot.ask", side_effect=slow) as provider:
            task = asyncio.create_task(self.bot.on_message(make_message("one", 100, FakeChannel(), mentions=[self.bot.user])))
            await started.wait()
            await self.bot.on_message(make_message("two", 101, FakeChannel(99), mentions=[self.bot.user]))
            self.assertEqual(provider.await_count, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.bot.inflight_users, set())

    async def test_global_admission_is_fail_fast(self):
        self.bot.inflight_users = {999}
        with patch("bot.MAX_INFLIGHT", 1), patch("bot.ask", AsyncMock()) as provider:
            await self.bot.on_message(make_message("hi", 100, FakeChannel(), mentions=[self.bot.user]))
            provider.assert_not_awaited()

    async def test_owner_pause_cannot_be_used_by_other_members(self):
        message = make_message("!security pause", 100, FakeChannel())
        await self.bot.on_message(message)
        self.assertEqual(self.store.get_setting("api_paused"), "")


class NetworkBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_resolution_never_connects_voice(self):
        from music import _play_or_restart
        channel = SimpleNamespace(id=7)
        message = SimpleNamespace(guild=SimpleNamespace(id=11, voice_client=None), author=SimpleNamespace(id=33, voice=SimpleNamespace(channel=channel)))
        with patch("music.resolve_music", AsyncMock(side_effect=ValueError("bad media"))), patch("music._connect_to_author", AsyncMock()) as connect:
            await _play_or_restart(SimpleNamespace(music_tracks={}), message, "bad", verb="playing")
        connect.assert_not_awaited()

    async def test_erasure_during_provider_response_does_not_restore_content(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def post(*args, **kwargs):
            started.set()
            await finish.wait()
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"choices": [{"message": {"content": "private sentinel"}}]})
        with tempfile.TemporaryDirectory() as folder:
            memory = MemoryStore(Path(folder)/"db")
            task = asyncio.create_task(ask(SimpleNamespace(post=post), memory, event_id="1", scope_id="c", user_id="u", server_id="g", prompt="hello", image_urls=[], persona="rudeish", created_at=time.time()))
            await started.wait()
            memory.erase_user_memory("u")
            finish.set()
            self.assertIsNone(await task)
            self.assertEqual(memory.recent_messages("c", "u", limit=10), [])

    async def test_downloader_rejects_destinations_before_network(self):
        for url in ("http://r1.googlevideo.com/a", "https://127.0.0.1/a", "https://evil.test/a", "https://r1.googlevideo.com.evil.test/a", "https://cdn.discordapp.com:444/a", "https://[bad/a"):
            with self.subTest(url=url), patch("music.httpx.AsyncClient") as client:
                with self.assertRaises(ValueError):
                    await download_audio({"url": url})
                client.assert_not_called()

    async def test_twitter_downloader_rejects_non_twitter_media_before_network(self):
        for url in ("https://evil.twimg.com/video.mp4", "https://r1.googlevideo.com/audio"):
            with self.subTest(url=url), patch("music.httpx.AsyncClient") as client:
                with self.assertRaises(ValueError):
                    await download_audio({"source": "twitter", "url": url})
                client.assert_not_called()

    async def test_redirect_and_size_limit_are_enforced(self):
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"OggS" + b"x" * 50
        original = httpx.AsyncClient
        def factory(handler):
            return lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
        track = {"source": "attachment", "url": "https://cdn.discordapp.com/attachments/a.ogg"}
        for response in (httpx.Response(302, headers={"location": "http://127.0.0.1"}), httpx.Response(200, stream=Stream())):
            calls = []
            def handler(request):
                calls.append(request)
                return response
            with patch("music.httpx.AsyncClient", side_effect=factory(handler)), patch("music.MAX_MEDIA_BYTES", 20):
                with self.assertRaises(ValueError):
                    await download_audio(track)
            self.assertEqual(len(calls), 1)

    async def test_playlist_disguised_as_audio_is_rejected(self):
        with self.assertRaises(ValueError):
            media_format(b"#EXTM3U\nhttp://127.0.0.1/secret")

    async def test_cancelled_resolver_kills_and_reaps_worker(self):
        async def communicate():
            raise asyncio.CancelledError()
        process = SimpleNamespace(returncode=None, communicate=communicate, kill=Mock(), wait=AsyncMock())
        with patch("music.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.CancelledError):
                await resolve_music("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_voice_controls_require_same_channel(self):
        voice = SimpleNamespace(channel=SimpleNamespace(id=7), stop=Mock(), pause=Mock(), disconnect=AsyncMock())
        message = SimpleNamespace(guild=SimpleNamespace(id=11, voice_client=voice), author=SimpleNamespace(voice=None), attachments=[])
        bot = SimpleNamespace(music_tracks={})
        for action in ("leave", "pause", "stop", "skip", "resume", "restart"):
            answer = await handle_music_command(bot, message, action)
            self.assertIn("current voice channel", answer)
        voice.stop.assert_not_called()
        voice.pause.assert_not_called()
        voice.disconnect.assert_not_awaited()

    async def test_paid_transport_fails_closed_on_storage_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = MemoryStore(Path(folder)/"db")
            http = SimpleNamespace(post=AsyncMock())
            with patch.object(memory, "reserve_api_request", side_effect=sqlite3.OperationalError("disk full")):
                with self.assertRaises(sqlite3.OperationalError):
                    await ask(http, memory, event_id="1", scope_id="c", user_id="u", server_id="g", prompt="hi", image_urls=[], persona="rudeish", created_at=time.time())
            http.post.assert_not_awaited()


class CacheTests(unittest.TestCase):
    def test_pynacl_upgrade_preserves_discord_packet_encryption(self):
        import discord
        import nacl.secret
        client = object.__new__(discord.VoiceClient)
        client._connection = SimpleNamespace(secret_key=list(range(32)))
        client._incr_nonce = 0
        header = bytes(12)
        packet = client._encrypt_aead_xchacha20_poly1305_rtpsize(header, b"audio")
        nonce = packet[-4:] + bytes(20)
        self.assertEqual(nacl.secret.Aead(bytes(client.secret_key)).decrypt(packet[12:-4], header, nonce), b"audio")

    def test_event_guard_stops_growing_at_capacity(self):
        guard = MessageEventGuard()
        guard.capacity = 2
        self.assertTrue(guard.claim(1, now=0))
        self.assertTrue(guard.claim(2, now=0))
        self.assertFalse(guard.claim(3, now=0))
        self.assertEqual(len(guard._seen), 2)


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg integration requires executable on PATH")
class NativeAudioTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux RLIMIT integration runs in CI")
    def test_valid_wave_decodes_with_pipe_only_protocols(self):
        from music import BoundedAudio, _ACTIVE_SOURCES
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(b"\0\0" * 4800)
        source = BoundedAudio(output.getvalue())
        try:
            self.assertTrue(source.read())
        finally:
            source.cleanup()
        self.assertNotIn(source, _ACTIVE_SOURCES)

    @unittest.skipIf(sys.platform.startswith("linux"), "Unsupported-host check is for non-Linux")
    def test_unsupported_host_refuses_native_playback(self):
        from music import BoundedAudio
        with self.assertRaisesRegex(ValueError, "Linux"):
            BoundedAudio(b"OggSdata")

    def test_failed_voice_play_cleans_up_decoder(self):
        from music import play_track
        source = Mock()
        with patch("music.BoundedAudio", return_value=source):
            with self.assertRaises(RuntimeError):
                play_track(SimpleNamespace(play=Mock(side_effect=RuntimeError("disconnected"))), {"audio_bytes": b"OggSdata"})
        source.cleanup.assert_called_once()
