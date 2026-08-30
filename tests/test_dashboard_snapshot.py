from __future__ import annotations

import types
import unittest
from unittest import mock

from owaua import dashboard_snapshot


class FakeChannel:
    def __init__(self, channel_id: int, *, everyone_view: bool, bot_send: bool) -> None:
        self.id = channel_id
        self.name = f"channel-{channel_id}"
        self.type = "text"
        self._everyone_view = everyone_view
        self._bot_send = bot_send

    def permissions_for(self, target: object) -> types.SimpleNamespace:
        if getattr(target, "is_default", lambda: False)():
            return types.SimpleNamespace(view_channel=self._everyone_view)
        return types.SimpleNamespace(view_channel=True, send_messages=self._bot_send)


class FakeRole:
    def __init__(self, role_id: int, *, default: bool = False) -> None:
        self.id = role_id
        self.name = f"role-{role_id}"
        self.color = "#123456"
        self.permissions = types.SimpleNamespace(value=8)
        self._default = default

    def is_default(self) -> bool:
        return self._default


def fake_member(
    member_id: int,
    *,
    name: str,
    bot: bool = False,
    administrator: bool = False,
    manage_guild: bool = False,
    boosting: bool = False,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=member_id,
        display_name=name,
        bot=bot,
        premium_since=object() if boosting else None,
        guild_permissions=types.SimpleNamespace(
            value=16,
            administrator=administrator,
            manage_guild=manage_guild,
        ),
    )


class DashboardSnapshotTests(unittest.TestCase):
    def test_snapshot_preserves_dashboard_fields_and_privacy_filters(self) -> None:
        everyone = FakeRole(1, default=True)
        owner = fake_member(10, name="o" * 120, boosting=True)
        manager = fake_member(11, name="manager", manage_guild=True)
        ordinary = fake_member(12, name="ordinary")
        bot_member = fake_member(13, name="bot", bot=True)
        guild = types.SimpleNamespace(
            id=100,
            name="Test Guild",
            icon=types.SimpleNamespace(url="https://cdn.example/icon.png"),
            member_count=4,
            default_role=everyone,
            me=bot_member,
            owner_id=owner.id,
            members=[owner, manager, ordinary, bot_member],
            channels=[
                FakeChannel(20, everyone_view=True, bot_send=True),
                FakeChannel(21, everyone_view=False, bot_send=False),
            ],
            roles=[everyone, FakeRole(30)],
        )

        snapshot = dashboard_snapshot.serialize_guilds([guild])

        self.assertEqual(len(snapshot), 1)
        item = snapshot[0]
        self.assertEqual(item["id"], "100")
        self.assertEqual(item["name"], "Test Guild")
        self.assertEqual(item["icon"], "https://cdn.example/icon.png")
        self.assertEqual(item["member_count"], 4)
        self.assertEqual(item["everyone_permissions"], 8)
        self.assertEqual(item["bot_permissions"], 16)
        self.assertEqual([member["id"] for member in item["members"]], ["10", "11", "12"])
        self.assertEqual(len(item["members"][0]["name"]), 100)
        self.assertTrue(item["members"][0]["boosting"])
        self.assertEqual(item["manager_ids"], ["10", "11"])
        self.assertEqual(
            [(channel["private"], channel["bot_writable"]) for channel in item["channels"]],
            [(False, True), (True, False)],
        )
        self.assertEqual(item["roles"], [{"id": "30", "name": "role-30", "color": "#123456"}])

    def test_snapshot_enforces_each_collection_bound(self) -> None:
        everyone = FakeRole(1, default=True)
        member = fake_member(10, name="owner")
        guild = types.SimpleNamespace(
            id=100,
            name="First",
            icon=None,
            member_count=None,
            default_role=everyone,
            me=None,
            owner_id=member.id,
            members=[member, fake_member(11, name="second")],
            channels=[
                FakeChannel(20, everyone_view=True, bot_send=True),
                FakeChannel(21, everyone_view=True, bot_send=True),
            ],
            roles=[FakeRole(30), FakeRole(31)],
        )
        second_guild = types.SimpleNamespace(**{**guild.__dict__, "id": 101, "name": "Second"})

        with (
            mock.patch.object(dashboard_snapshot, "MAX_GUILDS", 1),
            mock.patch.object(dashboard_snapshot, "MAX_MEMBERS", 1),
            mock.patch.object(dashboard_snapshot, "MAX_CHANNELS", 1),
            mock.patch.object(dashboard_snapshot, "MAX_ROLES", 1),
        ):
            snapshot = dashboard_snapshot.serialize_guilds([guild, second_guild])

        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["name"], "First")
        self.assertEqual(len(snapshot[0]["members"]), 1)
        self.assertEqual(len(snapshot[0]["channels"]), 1)
        self.assertEqual(len(snapshot[0]["roles"]), 1)
        self.assertEqual(snapshot[0]["bot_permissions"], 0)
        self.assertFalse(snapshot[0]["channels"][0]["bot_writable"])
