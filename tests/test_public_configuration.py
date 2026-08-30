"""Public-source defaults must not carry a specific deployment's identifiers."""

from __future__ import annotations

import unittest
from pathlib import Path

from owaua import config


class PublicConfigurationTest(unittest.TestCase):
    def test_deployment_specific_defaults_are_empty(self) -> None:
        source = Path(config.__file__).read_text(encoding="utf-8")
        self.assertEqual(config.SYNC_GUILDS, [])
        self.assertEqual(config.ARCHIVE_GUILD_IDS, frozenset())
        self.assertEqual(config.BLOCKED_USER_IDS, set())
        self.assertNotIn("1535083112709496903", source)
        self.assertNotIn("836988339491962881", source)
        self.assertNotIn("kozzyx.org", source)
        self.assertNotIn("https://wearegays.net", source)


if __name__ == "__main__":
    unittest.main()
