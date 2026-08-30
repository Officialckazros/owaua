import types
import unittest
from unittest import mock

from owaua import bot


class PrefixNukeTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_nuke_posts_invoker_bound_confirmation_preview(self):
        class FakeChannel:
            id = 456

            def permissions_for(self, _member):
                return types.SimpleNamespace(manage_messages=True, administrator=False)

            async def purge(self, *, limit, reason):
                return [object()] * limit

        class FakeMember:
            id = 123
            guild_permissions = types.SimpleNamespace()
            guild = types.SimpleNamespace(owner_id=999)

        channel = FakeChannel()
        member = FakeMember()
        guild = types.SimpleNamespace(
            id=789,
            me=member,
            get_channel=lambda channel_id: channel if channel_id == channel.id else None,
        )
        message = types.SimpleNamespace(
            guild=guild,
            author=member,
            channel=channel,
            id=111,
        )

        with (
            mock.patch.object(bot.discord, "Member", FakeMember),
            mock.patch.object(bot.discord, "TextChannel", FakeChannel),
            mock.patch.object(bot.discord, "Thread", FakeChannel),
            mock.patch.object(bot, "_send", new_callable=mock.AsyncMock) as send,
            mock.patch.object(bot.db, "record_action_audit"),
        ):
            await bot._cmd_nuke(message, "25", "789", "123")

        send.assert_awaited_once()
        view = send.await_args.kwargs["view"]
        self.assertEqual(view.actor_id, 123)
        self.assertEqual(view.guild_id, 789)
        self.assertEqual(view.channel_id, 456)
        self.assertIn("25", send.await_args.args[1].description)
