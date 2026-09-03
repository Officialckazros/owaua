import os
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from owaua import bot


class PingResponseTest(unittest.TestCase):
    def setUp(self):
        bot._ping_prompt_counts.clear()

    def test_response_is_eligible_on_every_fifteenth_prompt(self):
        with mock.patch.object(bot.secrets, "randbelow", return_value=0):
            for _ in range(14):
                self.assertFalse(bot._should_send_ping_response("user-1"))
            self.assertTrue(bot._should_send_ping_response("user-1"))
            self.assertFalse(bot._should_send_ping_response("user-1"))

    def test_eligible_response_can_fail_the_five_percent_roll(self):
        with mock.patch.object(bot.secrets, "randbelow", return_value=99):
            for _ in range(14):
                self.assertFalse(bot._should_send_ping_response("user-2"))
            self.assertFalse(bot._should_send_ping_response("user-2"))

    def test_cooldowns_are_isolated_per_user(self):
        with mock.patch.object(bot.secrets, "randbelow", return_value=0):
            for _ in range(14):
                bot._should_send_ping_response("user-3")
            self.assertTrue(bot._should_send_ping_response("user-3"))
            self.assertFalse(bot._should_send_ping_response("user-4"))
