import unittest
from concurrent.futures import ThreadPoolExecutor

from owaua import config, db, swearjar
from owaua.module_catalog import public_server_settings


class SwearJarTests(unittest.TestCase):
    def setUp(self):
        db.close()
        self.previous_db = config.DB_PATH
        config.DB_PATH = ":memory:"

    def tearDown(self):
        db.close()
        config.DB_PATH = self.previous_db

    def test_counts_each_swear_without_substring_false_positives(self):
        self.assertEqual(swearjar.count_swears("fuck this shitty bullshit"), 3)
        self.assertEqual(swearjar.count_swears("classic assignment cockpit"), 0)
        self.assertEqual(swearjar.count_swears("FUCK, fuck, and wtf"), 3)

    def test_requested_words_and_common_variants_are_counted(self):
        requested = (
            "fuck fucking fucked fucker motherfucker shit shitty bullshit bitch "
            "bastard asshole arsehole dick dickhead cock cunt prick piss pissed "
            "wanker twat bollocks bloody damn goddamn crap douche douchebag jackass "
            "dumbass badass suck sucks slut whore"
        )
        self.assertEqual(swearjar.count_swears(requested), 35)
        self.assertGreaterEqual(
            swearjar.count_swears("asshat asswipe bellend bugger dipshit fuckwit hell shithead tosser"),
            9,
        )

    def test_case_repetition_and_obfuscation_are_counted(self):
        self.assertEqual(swearjar.count_swears("F.U.C.K fuuuck shiiiiit b.i.t.c.h"), 4)
        self.assertEqual(swearjar.count_swears("assignment cockpit classic"), 0)

    def test_total_is_atomic_and_server_scoped(self):
        self.assertEqual(db.swear_jar_increment("guild:1", "42", 2), 2)
        self.assertEqual(db.swear_jar_increment("1", "42", 3), 5)
        self.assertEqual(db.swear_jar_count("guild:2", "42"), 0)

    def test_concurrent_increments_do_not_lose_counts(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _index: db.swear_jar_increment("1", "42", 1), range(50)))
        self.assertEqual(db.swear_jar_count("guild:1", "42"), 50)

    def test_dashboard_setting_defaults_off_and_can_be_enabled(self):
        self.assertFalse(db.guild_settings("guild:1")["swear_jar_enabled"])
        schema = {field["key"]: field for field in public_server_settings()}
        self.assertEqual(schema["swear_jar_enabled"]["label"], "Swear jar")
        self.assertTrue(
            db.dashboard_guild_settings_set(
                "guild:1",
                {"swear_jar_enabled": True},
                actor_id="dashboard-test",
            )["swear_jar_enabled"]
        )

    def test_privacy_export_and_delete_include_totals(self):
        db.swear_jar_increment("guild:1", "42", 4)
        self.assertEqual(db.privacy_export("42")["swear_jar_counts"][0]["count"], 4)
        self.assertEqual(db.privacy_delete_user("42")["swear_jar_counts"], 1)
        self.assertEqual(db.swear_jar_count("guild:1", "42"), 0)


if __name__ == "__main__":
    unittest.main()
