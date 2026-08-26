import unittest

from sefbot import config, db
from sefbot.module_catalog import MODULES


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

        self.assertFalse(
            db.module_config("123456789012345678", "levels")["enabled"]
        )
        self.assertTrue(
            db.module_config("123456789012345678", "welcome")["enabled"]
        )


if __name__ == "__main__":
    unittest.main()
