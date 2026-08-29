"""Regression coverage for automatic, scoped long-term memory learning."""

from __future__ import annotations

import os
import tempfile
import typing
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DISCORD_TOKEN", "test-token")

from owaua import ai, brain, config, db, kb
from owaua.scope import Scope


class MemoryLearningTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        db.close()
        self._old_db_path = config.DB_PATH
        self._tempdir = tempfile.TemporaryDirectory()
        config.DB_PATH = str(Path(self._tempdir.name) / "memory.sqlite3")
        db._gs_cache.clear()
        db._mem_cache.clear()
        db._lessons_cache = None
        db._lessons_ts = 0.0
        kb._READY = False
        kb._HAS_FTS5 = None
        self.user = "1172433512364769342"
        self.scope = Scope.guild(1535083112709496903).key

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

    async def test_explicit_remember_survives_extractor_failure(self) -> None:
        db.privacy_set_opt_in(self.user, self.scope, True)
        with mock.patch.object(ai, "json_call", side_effect=RuntimeError("provider down")):
            learned = await brain.learn_from_turn(
                "remember that my favorite game is Cyberpunk 2077",
                self.user,
                self.scope,
            )

        self.assertEqual(1, learned)
        rows = db.memories_about(self.user, self.scope)
        self.assertEqual(["my favorite game is Cyberpunk 2077"], [r["content"] for r in rows])
        self.assertEqual(0.9, rows[0]["importance"])

    async def test_model_extractor_keeps_multiple_durable_facts_for_current_user(self) -> None:
        db.privacy_set_opt_in(self.user, self.scope, True)
        result = {
            "memories": [
                {
                    "about": "server",
                    "content": "Owns and maintains owaua",
                    "importance": 0.9,
                },
                {
                    "about": "999999999999999999",
                    "content": "Prefers concise status updates",
                    "importance": 0.7,
                },
            ]
        }
        with mock.patch.object(ai, "json_call", new=mock.AsyncMock(return_value=result)):
            learned = await brain.learn_from_turn(
                "I own owaua and I prefer concise status updates",
                self.user,
                self.scope,
            )

        self.assertEqual(2, learned)
        rows = db.memories_about(self.user, self.scope)
        self.assertEqual(2, len(rows))
        self.assertEqual({self.user}, {row["subject"] for row in rows})

    async def test_opt_out_blocks_extraction_and_provider_call(self) -> None:
        provider = mock.AsyncMock(return_value={"memories": [{"content": "should not exist"}]})
        with mock.patch.object(ai, "json_call", new=provider):
            learned = await brain.learn_from_turn(
                "I own owaua and want that remembered",
                self.user,
                self.scope,
            )

        self.assertEqual(0, learned)
        provider.assert_not_awaited()
        self.assertEqual([], db.memories_about(self.user, self.scope))

    async def test_credentials_are_never_auto_stored(self) -> None:
        db.privacy_set_opt_in(self.user, self.scope, True)
        result = {
            "memories": [
                {"content": "My API key is sk-secret-value", "importance": 1.0},
                {"content": "Uses Python for bot development", "importance": 0.7},
            ]
        }
        with mock.patch.object(ai, "json_call", new=mock.AsyncMock(return_value=result)):
            await brain.learn_from_turn(
                "I use Python for bot development",
                self.user,
                self.scope,
            )

        self.assertEqual(
            ["Uses Python for bot development"],
            [row["content"] for row in db.memories_about(self.user, self.scope)],
        )

    async def test_extractor_can_recover_recent_user_context_without_bot_text(self) -> None:
        db.privacy_set_opt_in(self.user, self.scope, True)
        db.guild_settings_set(self.scope, history_enabled=True)
        db.convo_add(self.user, self.scope, "user", "I build custom keyboards on weekends")
        db.convo_add(self.user, self.scope, "bot", "You should buy every switch ever made")
        provider = mock.AsyncMock(return_value={"memories": []})
        with mock.patch.object(ai, "json_call", new=provider):
            await brain.learn_from_turn(
                "What do you remember about me?",
                self.user,
                self.scope,
            )

        prompt = typing.cast(typing.Any, provider.await_args).args[1]
        self.assertIn("I build custom keyboards on weekends", prompt)
        self.assertNotIn("buy every switch", prompt)

    def test_relevant_fact_is_retrieved_even_below_high_importance_rows(self) -> None:
        db.privacy_set_opt_in(self.user, self.scope, True)
        for index in range(5):
            db.add_memory(
                f"Unrelated durable preference number {index}",
                self.user,
                self.scope,
                subject=self.user,
                importance=0.9,
            )
        db.add_memory(
            "Builds a backyard telescope with a sibling",
            self.user,
            self.scope,
            subject=self.user,
            importance=0.4,
        )

        facts = brain.facts_about_user(
            self.user,
            self.scope,
            query="How is the telescope project going?",
            k=3,
        )

        self.assertIn("Builds a backyard telescope with a sibling", facts)


if __name__ == "__main__":
    unittest.main()
