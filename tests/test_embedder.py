from __future__ import annotations

import tempfile
import typing
import unittest
from types import SimpleNamespace
from unittest import mock

from owaua import community, config, db


class _TextChannel:
    def __init__(self) -> None:
        self.id = 20
        self.send = mock.AsyncMock(return_value=SimpleNamespace(id=900))
        self.message = SimpleNamespace(edit=mock.AsyncMock())
        self.fetch_message = mock.AsyncMock(return_value=self.message)


class ManagedEmbedPublisherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        db.close()
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous_db = config.DB_PATH
        config.DB_PATH = self.tempdir.name + "/embedder.sqlite3"
        self.channel = _TextChannel()

        def get_channel(channel_id: int) -> _TextChannel | None:
            return self.channel if channel_id == self.channel.id else None

        self.guild: typing.Any = SimpleNamespace(
            id=123,
            get_channel=get_channel,
        )
        self.settings: dict[str, typing.Any] = {
            "embeds": [
                {
                    "id": "rules",
                    "enabled": True,
                    "title": "Rules",
                    "description": "Be decent.",
                    "channel_id": str(self.channel.id),
                    "color": "5865f2",
                    "footer": "Managed by owaua",
                    "image_url": "javascript:bad",
                    "thumbnail_url": "https://example.test/icon.png",
                }
            ],
            "published_message_ids": {},
            "published_payload_hashes": {},
        }
        db.module_config_set(
            str(self.guild.id),
            "embedder",
            enabled=True,
            settings=self.settings,
            actor_id="test",
        )

    async def asyncTearDown(self) -> None:
        db.close()
        config.DB_PATH = self.previous_db
        self.tempdir.cleanup()

    async def test_publish_then_update_in_place_without_duplicates(self) -> None:
        with mock.patch.object(community.discord, "TextChannel", _TextChannel):
            self.assertEqual(await community.publish_configured_embeds(self.guild), 1)
            self.channel.send.assert_awaited_once()
            sent_embed = typing.cast(typing.Any, self.channel.send.await_args).kwargs["embed"]
            self.assertEqual(sent_embed.title, "Rules")
            self.assertIsNone(sent_embed.image.url)
            self.assertEqual(sent_embed.thumbnail.url, "https://example.test/icon.png")

            self.assertEqual(await community.publish_configured_embeds(self.guild), 0)
            self.channel.fetch_message.assert_not_awaited()

            saved = db.module_config(str(self.guild.id), "embedder")["settings"]
            saved["embeds"][0]["description"] = "Be excellent."
            db.module_config_set(
                str(self.guild.id),
                "embedder",
                enabled=True,
                settings=saved,
                actor_id="test",
            )

            self.assertEqual(await community.publish_configured_embeds(self.guild), 1)
            self.channel.fetch_message.assert_awaited_once_with(900)
            self.channel.message.edit.assert_awaited_once()
            edited = self.channel.message.edit.await_args.kwargs["embed"]
            self.assertEqual(edited.description, "Be excellent.")


if __name__ == "__main__":
    unittest.main()
