import asyncio
import unittest
from unittest import mock

from owaua import boosters, config, db


class BoosterLedgerTests(unittest.TestCase):
    def setUp(self):
        db.close()
        self.previous_db = config.DB_PATH
        config.DB_PATH = ":memory:"

    def tearDown(self):
        db.close()
        config.DB_PATH = self.previous_db

    def test_system_events_are_deduplicated_and_count_lifetime_totals(self):
        first, changed = db.booster_record_event("guild:1", "42", "message-1")
        duplicate, duplicate_changed = db.booster_record_event("1", "42", "message-1")
        second, second_changed = db.booster_record_event("1", "42", "message-2")

        self.assertTrue(changed)
        self.assertFalse(duplicate_changed)
        self.assertTrue(second_changed)
        self.assertEqual(first["current_boosts"], 1)
        self.assertEqual(duplicate["current_boosts"], 1)
        self.assertEqual(second["current_boosts"], 2)
        self.assertEqual(second["all_time_boosts"], 2)
        self.assertEqual(
            db.booster_stats("guild:1"),
            {"current_boosts": 2, "all_time_boosts": 2, "current_boosters": 1, "all_time_boosters": 1},
        )

    @mock.patch("owaua.db.now", return_value=1000.0)
    def test_member_transition_and_matching_system_message_count_once(self, _now):
        imported, started = db.booster_record_sync(
            "guild:1", "42", boosted_since=900.0, source="member"
        )
        event, changed = db.booster_record_event("guild:1", "42", "message-1")

        self.assertTrue(started)
        self.assertFalse(changed)
        self.assertEqual(imported["first_boosted"], 900.0)
        self.assertEqual(event["current_boosts"], 1)
        self.assertEqual(event["all_time_boosts"], 1)

    def test_stop_restart_and_manual_correction(self):
        db.booster_record_event("1", "42", "message-1")
        stopped, did_stop = db.booster_record_stop("1", "42")
        restarted, did_restart = db.booster_record_sync("1", "42", source="member")
        adjusted = db.booster_adjust("1", "42", 2)
        reduced = db.booster_adjust("1", "42", -2)

        self.assertTrue(did_stop)
        self.assertFalse(stopped["active"])
        self.assertEqual(stopped["current_boosts"], 0)
        self.assertTrue(did_restart)
        self.assertEqual(restarted["all_time_boosts"], 2)
        self.assertEqual(adjusted["current_boosts"], 3)
        self.assertEqual(adjusted["all_time_boosts"], 4)
        self.assertEqual(reduced["current_boosts"], 1)
        self.assertEqual(reduced["all_time_boosts"], 2)

    def test_privacy_export_and_delete_include_booster_history(self):
        db.booster_record_event("1", "42", "message-1")
        exported = db.privacy_export("42")
        counts = db.privacy_delete_user("42")

        self.assertEqual(exported["booster_members"][0]["current_boosts"], 1)
        self.assertEqual(counts["booster_members"], 1)
        self.assertEqual(counts["booster_events"], 1)
        self.assertEqual(db.booster_stats("1")["all_time_boosters"], 0)

    def test_dashboard_defaults_cover_every_booster_system(self):
        settings = db.module_config("guild:1", "boosters")["settings"]

        for key in (
            "tracking_enabled", "greetings_enabled", "automatic_role_enabled",
            "personal_roles_enabled", "role_gifts_enabled", "boost_level_roles",
            "boost_age_roles", "private_channels_enabled", "mention_reactions_enabled",
            "emoji_restrictions", "stat_channels", "log_events", "manager_role_ids",
        ):
            self.assertIn(key, settings)

    def test_combined_age_duration_parser(self):
        self.assertEqual(boosters.age_seconds("1hour:20mins"), 4_800)
        self.assertEqual(boosters.age_seconds("1 year 2 weeks 3 days"), 33_004_800)
        self.assertIsNone(boosters.age_seconds("soon"))

    def test_dashboard_sync_queue_is_consumed_by_discord_scheduler(self):
        db.community_record_create(
            "booster_dashboard_action",
            "guild:1",
            {"action": "sync", "target_id": "", "actor_id": "manager"},
            due=db.now(),
        )
        guild = mock.Mock(id=1)
        client = mock.Mock(guilds=[guild])

        with mock.patch(
            "owaua.boosters.sync_guild", new=mock.AsyncMock(return_value=3)
        ) as sync:
            asyncio.run(boosters._dashboard_actions_tick(client))

        sync.assert_awaited_once_with(guild)
        records = db.community_records(
            "booster_dashboard_action", "guild:1", status=None, limit=10
        )
        self.assertEqual(records[0]["status"], "completed")
        self.assertEqual(records[0]["data"]["result"], "synchronized; 3 newly imported")


if __name__ == "__main__":
    unittest.main()
