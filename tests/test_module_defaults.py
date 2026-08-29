import unittest

from owaua import config, db
from owaua.module_catalog import MODULES, SERVER_SETTINGS, default_settings


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
        self.assertEqual(disabled_module_switches, [])


if __name__ == "__main__":
    unittest.main()
