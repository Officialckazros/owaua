import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

import discord

from sefbot import community, config, db
from sefbot.module_catalog import MODULES, merge_settings


class _Channel:
    def __init__(self, channel_id: int, name: str = "logs"):
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.category_id = None
        self.send = mock.AsyncMock()


class _Guild:
    def __init__(self):
        self.id = 123456789012345678
        self.name = "Test Server"
        self.log_channel = _Channel(20)
        self.source_channel = _Channel(21, "general")
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(view_audit_log=True, administrator=False)
        )

    def get_channel(self, channel_id):
        return {
            self.log_channel.id: self.log_channel,
            self.source_channel.id: self.source_channel,
        }.get(channel_id)

    def get_channel_or_thread(self, channel_id):
        return self.get_channel(channel_id)


class ActionLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        db.close()
        self.previous_db = config.DB_PATH
        config.DB_PATH = ":memory:"
        self.guild = _Guild()
        self.settings = dict(MODULES["action_log"]["settings"])
        self.settings["channel_id"] = str(self.guild.log_channel.id)
        db.module_config_set(
            str(self.guild.id),
            "action_log",
            enabled=True,
            settings=self.settings,
            actor_id="test",
        )

    async def asyncTearDown(self):
        db.close()
        config.DB_PATH = self.previous_db

    async def test_rich_event_contains_actor_target_reason_changes_and_event_id(self):
        actor = SimpleNamespace(
            id=42,
            name="Moderator",
            display_name="Moderator",
            mention="<@42>",
            roles=[],
            bot=False,
            display_avatar=SimpleNamespace(url="https://cdn.example.test/avatar.png"),
        )
        target = SimpleNamespace(id=99, name="old-role", mention="<@&99>")

        await community.event_log(
            self.guild,
            "role",
            "Role updated",
            "A role was updated.",
            actor=actor,
            target=target,
            reason="Routine cleanup",
            changes=["**Name:** old-role → new-role"],
            event_id=1234,
        )

        self.guild.log_channel.send.assert_awaited_once()
        embed = self.guild.log_channel.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Role updated")
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("<@42>", fields["Actor"])
        self.assertIn("<@&99>", fields["Target"])
        self.assertEqual(fields["Reason"], "Routine cleanup")
        self.assertIn("old-role → new-role", fields["Changes"])
        self.assertIn("Event ID: 1234", embed.footer.text)
        self.assertIsNotNone(embed.timestamp)

    async def test_category_toggle_suppresses_only_that_event_family(self):
        self.settings["reaction_events"] = False
        db.module_config_set(
            str(self.guild.id), "action_log", enabled=True,
            settings=self.settings, actor_id="test",
        )

        await community.event_log(
            self.guild, "reaction", "Reaction added", "A reaction changed."
        )

        self.guild.log_channel.send.assert_not_awaited()

    async def test_ignored_target_user_is_suppressed(self):
        self.settings["ignored_user_ids"] = ["99"]
        db.module_config_set(
            str(self.guild.id), "action_log", enabled=True,
            settings=self.settings, actor_id="test",
        )

        await community.event_log(
            self.guild, "member", "Member updated", "A member changed.",
            target=SimpleNamespace(id=99, roles=[]),
        )

        self.guild.log_channel.send.assert_not_awaited()

    async def test_audit_entry_formats_authoritative_actor_and_changes(self):
        actor = SimpleNamespace(
            id=42, name="Moderator", display_name="Moderator", mention="<@42>",
            roles=[], bot=False, display_avatar=None,
        )
        target = SimpleNamespace(id=77, name="rules", mention="<#77>")
        entry = SimpleNamespace(
            action=discord.AuditLogAction.channel_update,
            guild=self.guild,
            user=actor,
            target=target,
            reason="Rename",
            before=[("name", "old-rules")],
            after=[("name", "rules")],
            extra=None,
            id=9876,
        )

        await community.audit_entry_log(entry)

        embed = self.guild.log_channel.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Channel Update")
        self.assertIn("<@42>", embed.description)
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("old-rules", fields["Changes"])
        self.assertIn("rules", fields["Changes"])
        self.assertEqual(fields["Reason"], "Rename")

    async def test_bulk_delete_logs_actor_count_authors_and_bounded_sample(self):
        actor = SimpleNamespace(
            id=42, name="Moderator", display_name="Moderator", mention="<@42>",
            roles=[], bot=False, display_avatar=None,
        )
        author = SimpleNamespace(
            id=55, name="Member", display_name="Member", mention="<@55>",
            roles=[], bot=False,
        )
        messages = [
            SimpleNamespace(
                id=100 + index,
                guild=self.guild,
                channel=self.guild.source_channel,
                author=author,
                content=f"message {index}",
                attachments=[],
            )
            for index in range(3)
        ]
        entry = SimpleNamespace(id=7654, user=actor, reason="Cleanup", extra=None)

        with mock.patch(
            "sefbot.community.recent_audit_entry",
            new=mock.AsyncMock(return_value=entry),
        ):
            await community.bulk_message_delete(messages)

        embed = self.guild.log_channel.send.await_args.kwargs["embed"]
        self.assertIn("Deleted **3** messages", embed.description)
        self.assertIn("<@55>: 3", embed.description)
        self.assertIn("message 0", embed.description)
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("<@42>", fields["Actor"])
        self.assertEqual(fields["Reason"], "Cleanup")

    async def test_raw_bulk_delete_reports_uncached_messages(self):
        payload = SimpleNamespace(
            guild_id=self.guild.id,
            channel_id=self.guild.source_channel.id,
            message_ids={100, 101, 102},
            cached_messages=[],
        )
        client = SimpleNamespace(get_guild=lambda guild_id: self.guild)

        with mock.patch(
            "sefbot.community.recent_audit_entry",
            new=mock.AsyncMock(return_value=None),
        ):
            await community.raw_bulk_message_delete(client, payload)

        embed = self.guild.log_channel.send.await_args.kwargs["embed"]
        self.assertIn("Deleted **3** messages", embed.description)
        self.assertIn("**3** message(s) were not cached", embed.description)

    async def test_message_content_can_be_disabled_without_losing_metadata(self):
        self.settings["include_message_content"] = False
        db.module_config_set(
            str(self.guild.id), "action_log", enabled=True,
            settings=self.settings, actor_id="test",
        )
        author = SimpleNamespace(
            id=55, name="Member", display_name="Member", mention="<@55>",
            roles=[], bot=False,
        )
        message = SimpleNamespace(
            id=100, guild=self.guild, channel=self.guild.source_channel,
            author=author, content="private message body", attachments=[],
        )

        with mock.patch(
            "sefbot.community.recent_audit_entry",
            new=mock.AsyncMock(return_value=None),
        ):
            await community.message_delete(message)

        embed = self.guild.log_channel.send.await_args.kwargs["embed"]
        self.assertNotIn("private message body", embed.description)
        self.assertIn("**Author:**", embed.description)
        self.assertIn("**Channel:**", embed.description)

    async def test_deleted_previewable_media_is_relayed_for_discord_preview(self):
        author = SimpleNamespace(
            id=55, name="Member", display_name="Member", mention="<@55>",
            roles=[], bot=False,
        )
        attachment = SimpleNamespace(
            filename="clip.mp4", content_type="video/mp4",
            url="https://cdn.discordapp.com/attachments/1/2/clip.mp4",
            to_file=mock.AsyncMock(return_value=discord.File(BytesIO(b"video"), filename="clip.mp4")),
        )
        message = SimpleNamespace(
            id=100, guild=self.guild, channel=self.guild.source_channel,
            author=author, content="watch this", attachments=[attachment],
        )

        with mock.patch(
            "sefbot.community.recent_audit_entry",
            new=mock.AsyncMock(return_value=None),
        ):
            await community.message_delete(message)

        attachment.to_file.assert_awaited_once_with(use_cached=True)
        kwargs = self.guild.log_channel.send.await_args.kwargs
        self.assertEqual(kwargs["files"][0].filename, "clip.mp4")
        self.assertIn(attachment.url, kwargs["embed"].description)

    async def test_non_media_deleted_attachment_remains_a_link_only(self):
        author = SimpleNamespace(
            id=55, name="Member", display_name="Member", mention="<@55>",
            roles=[], bot=False,
        )
        attachment = SimpleNamespace(
            filename="report.pdf", content_type="application/pdf",
            url="https://cdn.discordapp.com/attachments/1/2/report.pdf",
            to_file=mock.AsyncMock(),
        )
        message = SimpleNamespace(
            id=100, guild=self.guild, channel=self.guild.source_channel,
            author=author, content="report", attachments=[attachment],
        )

        with mock.patch(
            "sefbot.community.recent_audit_entry",
            new=mock.AsyncMock(return_value=None),
        ):
            await community.message_delete(message)

        attachment.to_file.assert_not_awaited()
        kwargs = self.guild.log_channel.send.await_args.kwargs
        self.assertIsNone(kwargs["files"])
        self.assertIn(attachment.url, kwargs["embed"].description)

    def test_catalog_exposes_complete_typed_logging_controls(self):
        settings = MODULES["action_log"]["settings"]
        self.assertEqual(settings["channel_id"], "")
        self.assertFalse(any(
            key.endswith("_channel_id") for key in settings if key != "channel_id"
        ))
        for key in (
            "audit_events", "message_events", "member_events", "moderation_events",
            "voice_events", "role_events", "channel_events", "thread_events",
            "server_events", "reaction_events", "command_events", "include_message_content",
            "include_attachments", "include_audit_changes", "include_reasons",
            "include_bot_events", "bulk_delete_sample_size", "ignored_user_ids",
        ):
            self.assertIn(key, settings)
        self.assertGreaterEqual(len(discord.AuditLogAction), 67)
        self.assertTrue(all(community._audit_kind(action.name) for action in discord.AuditLogAction))
        self.assertIn("U+2B50", community._reaction_label("⭐"))
        safe = community._log_content("@everyone **fake audit field**")
        self.assertNotIn("@everyone", safe)
        self.assertIn(r"\*\*fake audit field\*\*", safe)

    def test_legacy_destination_migrates_to_global_channel(self):
        settings = merge_settings(
            "action_log",
            {"message_channel_id": "20", "voice_channel_id": "21"},
        )

        self.assertEqual(settings["channel_id"], "20")
        self.assertNotIn("message_channel_id", settings)


if __name__ == "__main__":
    unittest.main()
