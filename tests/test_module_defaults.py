import unittest

from owaua import config, db
from owaua.module_catalog import MODULES, SERVER_SETTINGS, default_settings, merge_settings


class ModuleDefaultTests(unittest.TestCase):
    def setUp(self):
        db.close()
        self.previous_db = config.DB_PATH
        config.DB_PATH = ":memory:"

    def tearDown(self):
        db.close()
        config.DB_PATH = self.previous_db

    def test_every_unconfigured_module_starts_enabled(self):
        configs = db.module_configs("123456789012345678")

        self.assertEqual({item["module"] for item in configs}, set(MODULES))
        self.assertTrue(all(item["enabled"] for item in configs))

    def test_explicitly_disabled_module_stays_disabled(self):
        db.module_config_set(
            "123456789012345678",
            "levels",
            enabled=False,
            settings={},
            actor_id="test",
        )

        self.assertFalse(db.module_config("123456789012345678", "levels")["enabled"])
        self.assertTrue(db.module_config("123456789012345678", "welcome")["enabled"])

    def test_feature_switches_start_enabled(self):
        disabled_server_switches = [
            key
            for key, definition in SERVER_SETTINGS.items()
            if key.endswith("_enabled") and definition["default"] is not True
        ]
        disabled_module_switches = [
            f"{module}.{key}"
            for module in MODULES
            for key, value in default_settings(module).items()
            if key.endswith("_enabled") and value is not True
        ]

        self.assertEqual(disabled_server_switches, [])
        self.assertEqual(
            disabled_module_switches,
            ["automod.max_caps_enabled", "automod.max_length_enabled"],
        )

    def test_automod_caps_and_length_checks_start_disabled(self):
        settings = default_settings("automod")

        self.assertFalse(settings["max_caps_enabled"])
        self.assertFalse(settings["max_length_enabled"])

    def test_legacy_automod_thresholds_remain_opted_in(self):
        merged = merge_settings("automod", {"max_caps_percent": 80, "max_length": 1800})

        self.assertTrue(merged["max_caps_enabled"])
        self.assertTrue(merged["max_length_enabled"])


if __name__ == "__main__":
    unittest.main()
