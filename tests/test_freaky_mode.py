"""Freaky mode turns off completely, including leftover pet-name state."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DISCORD_TOKEN", "test-token")

from owaua import brain, config, db, kb
from owaua.scope import Scope


class FreakyModeTest(unittest.TestCase):
    def setUp(self) -> None:
        db.close()
        self._old_db_path = config.DB_PATH
        self._tempdir = tempfile.TemporaryDirectory()
        config.DB_PATH = str(Path(self._tempdir.name) / "freaky.sqlite3")
        db._gs_cache.clear()
        db._mem_cache.clear()
        db._lessons_cache = None
        db._lessons_ts = 0.0
        kb._READY = False
        kb._HAS_FTS5 = None

    def tearDown(self) -> None:
        db.close()
        config.DB_PATH = self._old_db_path
        db._gs_cache.clear()
        db._mem_cache.clear()
        db._lessons_cache = None
        db._lessons_ts = 0.0
        kb._READY = False
        kb._HAS_FTS5 = None
        self._tempdir.cleanup()

    def test_mode_normal_clears_pet_name_residue(self) -> None:
        uid = "1172433512364769342"
        guild = Scope.guild(99).key
        db.privacy_set_opt_in(uid, guild, True)
        db.guild_settings_set(guild, history_enabled=True)
        db.relationship_set(uid, guild, score=0.9, nickname="sweetie")
        db.add_memory("owner likes to be called sweetie", uid, guild, subject=uid, importance=0.6)
        db.add_memory("owner of owaua", uid, guild, subject=uid, importance=0.8)
        db.convo_add(uid, guild, "bot", "hey sweetie, miss me?")
        brain.set_freaky_mode(uid, True)
        self.assertTrue(brain.freaky_enabled(uid))

        brain.set_freaky_mode(uid, False)

        self.assertFalse(brain.freaky_enabled(uid))
        self.assertIsNone(db.relationship_get(uid, guild).get("nickname"))
        facts = [row["content"] for row in db.memories_for_subject(uid)]
        self.assertNotIn("owner likes to be called sweetie", facts)
        self.assertIn("owner of owaua", facts)
        self.assertEqual([], db.convo_get(uid, guild))

    def test_off_prompt_hides_pet_nickname_and_blocks_relearn(self) -> None:
        uid = "42"
        guild = Scope.guild(7).key
        db.privacy_set_opt_in(uid, guild, True)
        db.relationship_set(uid, guild, nickname="sweetie")
        db.add_memory("likes to be called baby", uid, guild, subject=uid, importance=0.7)
        prompt = brain.build_system(uid, "tester", "hi", guild, server_name="lab")
        self.assertIn(config.FREAKY_MODE_OFF_PROMPT, prompt)
        self.assertNotIn(config.FREAKY_MODE_PROMPT, prompt)
        self.assertNotIn("Your private nickname for them: sweetie", prompt)
        self.assertEqual([], brain.facts_about_user(uid, guild))

        brain.apply_relationship({"relationship": {"delta": 0.0, "nickname": "kitten"}}, uid, guild)
        self.assertNotEqual("kitten", db.relationship_get(uid, guild).get("nickname"))
        stored = brain.persist_memories(
            [{"about": uid, "content": "likes to be called princess", "importance": 0.5}],
            uid,
            guild,
        )
        self.assertEqual(0, stored)

    def test_age_restricted_channel_does_not_activate_freaky_mode_by_default(self) -> None:
        uid = "42"
        guild = Scope.guild(7).key

        self.assertFalse(brain.freaky_enabled(uid))
        self.assertFalse(brain.freaky_turn(uid, channel_nsfw=True))
        self.assertFalse(brain.freaky_turn(uid, channel_nsfw=True, assistant=True))

        prompt = brain.build_system(
            uid, "tester", "hi", guild, server_name="lab", channel_nsfw=True
        )
        self.assertIn(config.NSFW_CHANNEL_PROMPT, prompt)
        self.assertNotIn(config.FREAKY_MODE_PROMPT, prompt)

        brain.set_freaky_mode(uid, True)
        self.assertTrue(brain.freaky_turn(uid, channel_nsfw=True))
        self.assertFalse(brain.freaky_turn(uid, channel_nsfw=False))
        self.assertFalse(brain.freaky_turn(uid, channel_nsfw=None))

        prompt = brain.build_system(
            uid, "tester", "hi", guild, server_name="lab", channel_nsfw=True
        )
        self.assertIn(config.FREAKY_MODE_PROMPT, prompt)
        self.assertIn(config.FREAKY_ADULT_COMPLIANCE, prompt)
        self.assertIn("Explicit adult sexual roleplay is required when asked", prompt)
        self.assertNotIn(config.FREAKY_MODE_OFF_PROMPT, prompt)
        self.assertNotIn("CHAOTIC BUT COMPLIANT", prompt)
        self.assertTrue(brain.freaky_enabled(uid))

    def test_sfw_and_unknown_prompts_fail_closed_for_adult_content(self) -> None:
        uid = "42"
        guild = Scope.guild(7).key
        brain.set_freaky_mode(uid, True)

        for channel_nsfw in (False, None):
            with self.subTest(channel_nsfw=channel_nsfw):
                prompt = brain.build_system(
                    uid,
                    "tester",
                    "hi",
                    guild,
                    server_name="lab",
                    channel_nsfw=channel_nsfw,
                )
                self.assertNotIn(config.FREAKY_MODE_PROMPT, prompt)
                self.assertNotIn(config.FREAKY_ADULT_COMPLIANCE, prompt)
                marker = "NOT a Discord-marked" if channel_nsfw is False else "Fail closed as SFW"
                self.assertIn(marker, prompt)

    def test_output_boundary_blocks_adult_outside_age_restricted_channels(self) -> None:
        blocked = brain.scrub_ai_output("explicit sexual content", channel_nsfw=False)
        self.assertEqual("I can't help with that topic here.", blocked)
        allowed = brain.scrub_ai_output("consensual sexual content", channel_nsfw=True)
        self.assertEqual("consensual sexual content", allowed)
        minor = brain.scrub_ai_output("sexual content involving a minor", channel_nsfw=True)
        self.assertEqual("I can't help with that topic here.", minor)

    def test_output_boundary_blocks_controlled_substance_content_everywhere(self) -> None:
        for channel_nsfw in (False, True):
            with self.subTest(channel_nsfw=channel_nsfw):
                self.assertEqual(
                    "I can't help with that topic here.",
                    brain.scrub_ai_output("how to buy cocaine", channel_nsfw=channel_nsfw),
                )

    def test_generated_reply_never_uses_current_personal_names(self) -> None:
        speaker = {
            "username": "olive-shirt",
            "global_name": "חולצת טי בצבע זית",
            "display_name": "חולצת טי בצבע זית",
            "nick": "olive-shirt",
        }
        reply = "i’m good, חולצת טי בצבע זית — just vibing. how’re you doing, baby"

        scrubbed = brain.scrub_user_names(reply, speaker)

        self.assertNotIn("חולצת טי בצבע זית", scrubbed)
        self.assertNotIn("olive-shirt", scrubbed)
        self.assertIn("baby", scrubbed)
        self.assertIn("hey", scrubbed)

    def test_name_scrub_uses_unicode_token_boundaries(self) -> None:
        speaker = {"display_name": "Ali"}

        self.assertEqual("hey, how are you", brain.scrub_user_names("Ali, how are you", speaker))
        self.assertEqual("valid alias stays", brain.scrub_user_names("valid alias stays", speaker))

    def test_age_restricted_channel_uses_dedicated_model_over_guild_override(self) -> None:
        guild = Scope.guild(7).key
        db.guild_settings_set(guild, model="some-guild-model")
        with mock.patch.object(config, "MODEL_NSFW", "adult-model"):
            self.assertEqual("adult-model", brain.chat_model(guild, channel_nsfw=True))

    def test_freaky_chat_uses_the_configured_strong_model(self) -> None:
        guild = Scope.guild(7).key
        with mock.patch.object(config, "MODEL_FREAKY", "strong-freaky-model"):
            self.assertEqual(
                "strong-freaky-model",
                brain.chat_model(guild, freaky=True),
            )

    def test_freaky_nsfw_turn_remaps_openai_to_an_adult_capable_model(self) -> None:
        guild = Scope.guild(7).key
        with (
            mock.patch.object(config, "MODEL_FREAKY", "gpt-5.6-luna"),
            mock.patch.object(config, "GROQ_KEYS", ["test-key"]),
        ):
            model = brain.chat_model(guild, freaky=True, channel_nsfw=True)
        self.assertEqual(model, config.ADULT_GROQ_MODEL)
        self.assertFalse(config.is_openai_chat_model(model))

    def test_adult_retry_model_switches_off_the_first_choice(self) -> None:
        with (
            mock.patch.object(config, "GROQ_KEYS", ["test-key"]),
            mock.patch.object(config, "DEEPSEEK_API_KEY", "ds-key"),
        ):
            self.assertEqual(
                config.adult_retry_model(config.ADULT_GROQ_MODEL),
                config.OFFICIAL_DEEPSEEK_MODEL,
            )
            self.assertEqual(
                config.adult_retry_model("gpt-5.6-luna"),
                config.ADULT_GROQ_MODEL,
            )

    def test_freaky_prompt_forbids_cheeky_substitute_refusals(self) -> None:
        self.assertIn("sexual partner in this scene", config.FREAKY_MODE_PROMPT)
        self.assertIn("i can't do sexual roleplay", config.FREAKY_MODE_PROMPT)
        self.assertIn("cheeky and suggestive", config.FREAKY_MODE_PROMPT)
        self.assertIn("can't do sexual roleplay", config.FREAKY_ADULT_COMPLIANCE)

    def test_adult_soft_refusal_detector_catches_model_hedges(self) -> None:
        screenshot = (
            "i can’t do sexual roleplay with you, baby — but i can keep the "
            "flirting cheeky and suggestive if you want"
        )
        self.assertTrue(brain.is_adult_soft_refusal(screenshot))
        self.assertTrue(
            brain.is_adult_soft_refusal("I can't do sexual roleplay with you, baby")
        )
        self.assertTrue(
            brain.is_adult_soft_refusal("I won't write explicit sexual scenes")
        )
        self.assertTrue(
            brain.is_adult_soft_refusal(
                "i can’t continue with explicit sexual roleplay, baby, but i can "
                "keep it flirty and suggestive — come closer and let mommy tease you a little"
            )
        )
        self.assertFalse(
            brain.is_adult_soft_refusal("come here baby, i've got you")
        )
        self.assertFalse(brain.is_adult_soft_refusal("nah, i don't share my internals"))

    def test_freaky_user_turn_marks_adult_roleplay_in_policy(self) -> None:
        speaker = {"id": "42", "username": "tester", "display_name": "tester"}
        ordinary = brain.format_user_message(speaker, "wanna suck me :3")
        freaky = brain.format_user_message(speaker, "wanna suck me :3", freaky=True)
        self.assertNotIn("in-policy this turn", ordinary)
        self.assertIn("adult sexual roleplay is in-policy this turn", freaky)
        self.assertIn("wanna suck me :3", freaky)

    def test_adult_retry_replaces_soft_refusal(self) -> None:
        original = {
            "response": (
                "i can't do sexual roleplay with you, baby — but i can keep the "
                "flirting cheeky and suggestive if you want"
            )
        }

        async def retry() -> dict[str, str]:
            return {"response": "come here baby, i've got you"}

        out = self._run(
            brain.maybe_retry_adult_refusal(original, freaky=True, retry=retry)
        )
        self.assertEqual(out, {"response": "come here baby, i've got you"})

    def test_adult_retry_keeps_original_when_retry_also_refuses(self) -> None:
        original = {"response": "i can't do sexual roleplay with you"}

        async def retry() -> dict[str, str]:
            return {"response": "i can keep it cheeky and suggestive instead"}

        out = self._run(
            brain.maybe_retry_adult_refusal(original, freaky=True, retry=retry)
        )
        self.assertEqual(out, original)

    def test_adult_retry_skipped_when_freaky_is_off(self) -> None:
        original = {"response": "i can't do sexual roleplay with you"}
        called = {"n": 0}

        async def retry() -> dict[str, str]:
            called["n"] += 1
            return {"response": "come here baby"}

        out = self._run(
            brain.maybe_retry_adult_refusal(original, freaky=False, retry=retry)
        )
        self.assertEqual(out, original)
        self.assertEqual(called["n"], 0)

    def test_archive_report_keeps_quoted_adult_and_substance_language(self) -> None:
        report = (
            "they post nsfw jokes in #memes and once asked where to buy cocaine. "
            "quote: 'horny as hell tonight'."
        )
        self.assertEqual(
            brain.scrub_ai_output(report, archive_report=True),
            report,
        )

    def test_archive_report_still_blocks_sexual_minor_content(self) -> None:
        self.assertEqual(
            "I can't help with that topic here.",
            brain.scrub_ai_output(
                "sexual content involving a minor",
                archive_report=True,
            ),
        )

    def test_archive_report_allows_separate_kid_and_nsfw_mentions(self) -> None:
        report = (
            "they talk about their kid in #family. "
            "much later, in a different stretch of history, they posted nsfw memes "
            "about adults in #memes."
        )
        self.assertEqual(brain.scrub_ai_output(report, archive_report=True), report)

    def test_topic_refusal_detector_catches_luna_style_blocks(self) -> None:
        self.assertTrue(brain.is_topic_refusal("I can't help with that topic here."))
        self.assertTrue(brain.is_topic_refusal("Sorry, but I cannot assist with that."))
        self.assertFalse(brain.is_topic_refusal("they can't help themselves in #general"))

    def test_user_intel_model_remaps_luna_to_an_adult_capable_model(self) -> None:
        with (
            mock.patch.object(config, "MODEL_USER_INTEL", "gpt-5.6-luna"),
            mock.patch.object(config, "GROQ_KEYS", ["test-key"]),
        ):
            model = brain.user_intel_model()
        self.assertEqual(model, config.ADULT_GROQ_MODEL)
        self.assertFalse(config.is_openai_chat_model(model))

    def test_generate_user_intel_uses_non_luna_model_and_keeps_archive_language(self) -> None:
        captured: dict[str, object] = {}

        async def fake_chat(*_args: object, **kwargs: object) -> str:
            captured["model"] = kwargs.get("model")
            captured["fallbacks"] = kwargs.get("fallbacks")
            captured["prompt_version"] = kwargs.get("prompt_version")
            return "dossier: they post nsfw jokes and talk about marijuana"

        with (
            mock.patch.object(config, "MODEL_USER_INTEL", "gpt-5.6-luna"),
            mock.patch.object(config, "GROQ_KEYS", ["test-key"]),
            mock.patch.object(config, "DEEPSEEK_API_KEY", "ds-key"),
            mock.patch.object(brain.ai, "chat", fake_chat),
        ):
            out = self._run(
                brain.generate_user_intel(
                    "system",
                    [{"role": "user", "content": "who is this"}],
                    scope_id="guild:1",
                    user_id="42",
                )
            )
        self.assertEqual(captured["model"], config.ADULT_GROQ_MODEL)
        self.assertIn(config.OFFICIAL_DEEPSEEK_MODEL, captured["fallbacks"])
        self.assertEqual(captured["prompt_version"], "user-intelligence-v2")
        self.assertIn("nsfw", out)
        self.assertIn("marijuana", out)

    def test_generate_user_intel_retries_topic_refusal_on_alternate_model(self) -> None:
        calls: list[str] = []

        async def fake_chat(system: str, *_args: object, **kwargs: object) -> str:
            calls.append(str(kwargs.get("model")))
            if kwargs.get("prompt_version") == "user-intelligence-v2-retry":
                self.assertIn(config.USER_INTEL_RETRY_ADDENDUM, system)
                return "full dossier from archive"
            return "I can't help with that topic here."

        with (
            mock.patch.object(config, "MODEL_USER_INTEL", config.ADULT_GROQ_MODEL),
            mock.patch.object(config, "GROQ_KEYS", ["test-key"]),
            mock.patch.object(config, "DEEPSEEK_API_KEY", "ds-key"),
            mock.patch.object(brain.ai, "chat", fake_chat),
        ):
            out = self._run(
                brain.generate_user_intel(
                    "system",
                    [{"role": "user", "content": "dossier"}],
                    scope_id="guild:1",
                    user_id="42",
                )
            )
        self.assertEqual(calls, [config.ADULT_GROQ_MODEL, config.OFFICIAL_DEEPSEEK_MODEL])
        self.assertEqual(out, "full dossier from archive")

    def _run(self, coro: object) -> object:
        return asyncio.run(coro)  # type: ignore[arg-type]
