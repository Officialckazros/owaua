"""Freaky mode turns off completely, including leftover pet-name state."""

from __future__ import annotations

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
        db.add_memory(
            "owner likes to be called sweetie", uid, guild, subject=uid, importance=0.6
        )
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
        db.add_memory(
            "likes to be called baby", uid, guild, subject=uid, importance=0.7
        )
        prompt = brain.build_system(uid, "tester", "hi", guild, server_name="lab")
        self.assertIn(config.FREAKY_MODE_OFF_PROMPT, prompt)
        self.assertNotIn(config.FREAKY_MODE_PROMPT, prompt)
        self.assertNotIn("Your private nickname for them: sweetie", prompt)
        self.assertEqual([], brain.facts_about_user(uid, guild))

        brain.apply_relationship(
            {"relationship": {"delta": 0.0, "nickname": "kitten"}}, uid, guild
        )
        self.assertNotEqual("kitten", db.relationship_get(uid, guild).get("nickname"))
        stored = brain.persist_memories(
            [{"about": uid, "content": "likes to be called princess", "importance": 0.5}],
            uid,
            guild,
        )
        self.assertEqual(0, stored)

    def test_age_restricted_channel_activates_adult_persona_without_saved_mode(self) -> None:
        uid = "42"
        guild = Scope.guild(7).key

        self.assertFalse(brain.freaky_enabled(uid))
        self.assertTrue(brain.freaky_turn(uid, channel_nsfw=True))
        self.assertFalse(brain.freaky_turn(uid, channel_nsfw=True, assistant=True))
        brain.set_freaky_mode(uid, True)
        self.assertFalse(brain.freaky_turn(uid, channel_nsfw=False))
        self.assertFalse(brain.freaky_turn(uid, channel_nsfw=None))

        prompt = brain.build_system(
            uid, "tester", "hi", guild, server_name="lab", channel_nsfw=True
        )
        self.assertIn(config.NSFW_CHANNEL_PROMPT, prompt)
        self.assertNotIn(config.FREAKY_MODE_OFF_PROMPT, prompt)
        self.assertTrue(brain.freaky_enabled(uid))

    def test_sfw_and_unknown_prompts_fail_closed_for_adult_content(self) -> None:
        uid = "42"
        guild = Scope.guild(7).key
        brain.set_freaky_mode(uid, True)

        for channel_nsfw in (False, None):
            with self.subTest(channel_nsfw=channel_nsfw):
                prompt = brain.build_system(
                    uid, "tester", "hi", guild,
                    server_name="lab", channel_nsfw=channel_nsfw,
                )
                self.assertNotIn(config.FREAKY_MODE_PROMPT, prompt)
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

    def test_age_restricted_channel_uses_dedicated_model_over_guild_override(self) -> None:
        guild = Scope.guild(7).key
        db.guild_settings_set(guild, model="some-guild-model")
        with mock.patch.object(config, "MODEL_NSFW", "adult-model"):
            self.assertEqual(
                "adult-model", brain.chat_model(guild, channel_nsfw=True)
            )
