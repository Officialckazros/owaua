"""Tests for the one-time public site/dashboard onboarding marker."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from owaua import config, db


class FirstUseNoticeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(config, "DB_PATH", str(Path(self.tempdir.name) / "state.db"))
        self.patch.start()
        db.close()

    def tearDown(self) -> None:
        db.close()
        self.patch.stop()
        self.tempdir.cleanup()

    def test_claim_is_one_time_and_privacy_delete_resets_it(self) -> None:
        self.assertTrue(db.claim_first_use_notice("123"))
        self.assertFalse(db.claim_first_use_notice("123"))
        self.assertTrue(db.claim_first_use_notice("456"))

        exported = db.privacy_export("123")
        self.assertEqual(len(exported["first_use_notice"]), 1)
        db.privacy_delete_user("123")
        self.assertTrue(db.claim_first_use_notice("123"))
