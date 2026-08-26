import json
import re
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from aiohttp.test_utils import TestClient, TestServer

from sefbot import config, db
from sefbot.dashboard import (
    _JS,
    DISCORD_GUILDS_JSON_BYTES,
    DashboardAuthConfig,
    _read_provider_json,
)
from sefbot.module_catalog import MODULES, SERVER_SETTINGS, default_settings, merge_settings
from sefbot.web import create_app


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        db.close()
        self.previous_db = config.DB_PATH
        config.DB_PATH = ":memory:"
        self.auth = DashboardAuthConfig(
            public_url="https://wearegays.net",
            session_secret="s" * 48,
            discord_client_id="123456789012345678",
            discord_client_secret="discord-secret",
        )
        self.app = create_app(
            privacy_contact="privacy@example.test",
            dashboard_auth=self.auth,
            guild_provider=lambda: [
                {
                    "id": "123456789012345678",
                    "name": "Test Server",
                    "member_count": 42,
                    "members": [
                        {"id": "40", "name": "Booster One", "boosting": True},
                        {"id": "41", "name": "Member Two", "boosting": False},
                    ],
                    "channels": [{"id": "20", "name": "general", "type": "text"}],
                    "roles": [{"id": "30", "name": "Moderator", "color": "#fff"}],
                }
            ],
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        db.close()
        config.DB_PATH = self.previous_db

    async def _login(self) -> tuple[str, str]:
        start = await self.client.get("/dashboard/auth/discord", allow_redirects=False)
        self.assertEqual(start.status, 303)
        state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
        nonce = start.cookies["sefbot_dashboard_auth_nonce"].value
        with mock.patch(
            "sefbot.dashboard._discord_identity",
            new=mock.AsyncMock(
                return_value=(
                    {"id": "555555555555555555"},
                    [{"id": "123456789012345678", "owner": True, "permissions": "0"}],
                )
            ),
        ):
            response = await self.client.get(
                "/dashboard/auth/discord/callback",
                params={"code": "c" * 20, "state": state},
                headers={"Cookie": f"sefbot_dashboard_auth_nonce={nonce}"},
                allow_redirects=False,
            )
        self.assertEqual(response.status, 303, await response.text())
        cookie = response.cookies["sefbot_dashboard_session"].value
        headers = {"Cookie": f"sefbot_dashboard_session={cookie}"}
        session_response = await self.client.get("/dashboard/api/session", headers=headers)
        self.assertEqual(session_response.status, 200)
        csrf = (await session_response.json())["csrf"]
        return headers["Cookie"], csrf

    async def test_dashboard_requires_auth_and_uses_strict_headers(self):
        response = await self.client.get("/dashboard")
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn("Continue with Discord", body)
        self.assertNotIn("Google", body)
        self.assertNotIn("email", body.lower())
        self.assertNotIn("password", body.lower())
        self.assertNotIn("Owner access", body)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertNotIn("gstatic.com", response.headers["Content-Security-Policy"])

        self.assertEqual((await self.client.post("/dashboard/login")).status, 404)
        self.assertEqual((await self.client.post("/dashboard/auth/firebase")).status, 404)
        css = await (await self.client.get("/dashboard/assets/app.css")).text()
        self.assertIn("--black:#000;--white:#fff", css)
        self.assertNotIn("gradient", css)
        self.assertNotIn("box-shadow", css)

        api = await self.client.get("/dashboard/api/catalog")
        self.assertEqual(api.status, 401)
        boosters = await self.client.get(
            "/dashboard/api/guild/123456789012345678/boosters"
        )
        self.assertEqual(boosters.status, 401)

    async def test_discord_oauth_failure_returns_to_login_instead_of_502(self):
        start = await self.client.get("/dashboard/auth/discord", allow_redirects=False)
        self.assertEqual(start.status, 303)
        state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
        nonce = start.cookies["sefbot_dashboard_auth_nonce"].value

        with mock.patch(
            "sefbot.dashboard._discord_identity",
            new=mock.AsyncMock(return_value=None),
        ):
            callback = await self.client.get(
                "/dashboard/auth/discord/callback",
                params={"code": "c" * 20, "state": state},
                headers={"Cookie": f"sefbot_dashboard_auth_nonce={nonce}"},
                allow_redirects=False,
            )

        self.assertEqual(callback.status, 303, await callback.text())
        self.assertEqual(callback.headers["Location"], "/dashboard?auth=discord_failed")
        self.assertEqual(callback.cookies["sefbot_dashboard_auth_nonce"]["max-age"], "0")

        login = await self.client.get("/dashboard?auth=discord_failed")
        self.assertEqual(login.status, 200)
        self.assertIn("Discord could not complete sign-in", await login.text())

    async def test_discord_guild_response_allows_a_bounded_large_list(self):
        payload = json.dumps([{"id": "1", "name": "x" * 1_100_000}]).encode()

        class Content:
            def __init__(self):
                self.offset = 0
                self.reads = 0

            async def read(self, limit):
                self.reads += 1
                if self.offset >= len(payload):
                    return b""
                size = min(limit, 16 * 1024)
                chunk = payload[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        class Response:
            def __init__(self):
                self.content = Content()

        response = Response()
        guilds = await _read_provider_json(
            response, limit=DISCORD_GUILDS_JSON_BYTES
        )

        self.assertIsInstance(guilds, list)
        self.assertEqual(guilds[0]["id"], "1")
        self.assertGreater(response.content.reads, 2)

    async def test_provider_json_rejects_a_chunked_oversized_response(self):
        class Content:
            async def read(self, limit):
                return b"x" * limit

        class Response:
            content = Content()

        self.assertIsNone(await _read_provider_json(Response(), limit=1024))

    async def test_discord_oauth_limits_visible_guilds(self):
        auth = DashboardAuthConfig(
            public_url="https://wearegays.net",
            session_secret="s" * 48,
            discord_client_id="123456789012345678",
            discord_client_secret="discord-secret",
        )
        app = create_app(
            privacy_contact="privacy@example.test",
            dashboard_auth=auth,
            guild_provider=lambda: [
                {
                    "id": "123456789012345678",
                    "name": "Managed",
                    "members": [{"id": "40", "name": "Private member roster"}],
                },
                {"id": "987654321098765432", "name": "Not managed"},
            ],
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)

        start = await client.get("/dashboard/auth/discord", allow_redirects=False)
        self.assertEqual(start.status, 303)
        state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
        nonce = start.cookies["sefbot_dashboard_auth_nonce"].value
        with mock.patch(
            "sefbot.dashboard._discord_identity",
            new=mock.AsyncMock(
                return_value=(
                    {"id": "555555555555555555"},
                    [
                        {
                            "id": "123456789012345678",
                            "owner": False,
                            "permissions": "32",
                        },
                        {
                            "id": "987654321098765432",
                            "owner": False,
                            "permissions": "0",
                        },
                    ],
                )
            ),
        ):
            callback = await client.get(
                "/dashboard/auth/discord/callback",
                params={"code": "c" * 20, "state": state},
                headers={"Cookie": f"sefbot_dashboard_auth_nonce={nonce}"},
                allow_redirects=False,
            )
        self.assertEqual(callback.status, 303, await callback.text())
        session_cookie = callback.cookies["sefbot_dashboard_session"].value
        guilds = await (
            await client.get(
                "/dashboard/api/guilds",
                headers={"Cookie": f"sefbot_dashboard_session={session_cookie}"},
            )
        ).json()
        self.assertEqual(
            [item["id"] for item in guilds["guilds"]], ["123456789012345678"]
        )
        self.assertNotIn("members", guilds["guilds"][0])

        modules = await (
            await client.get(
                "/dashboard/api/guild/123456789012345678/modules",
                headers={"Cookie": f"sefbot_dashboard_session={session_cookie}"},
            )
        ).json()
        settings = await (
            await client.get(
                "/dashboard/api/guild/123456789012345678/settings",
                headers={"Cookie": f"sefbot_dashboard_session={session_cookie}"},
            )
        ).json()
        self.assertNotIn("members", modules["guild"])
        self.assertNotIn("members", settings["guild"])

    async def test_module_configuration_round_trip_and_csrf(self):
        cookie, csrf = await self._login()
        headers = {"Cookie": cookie, "X-CSRF-Token": csrf, "Content-Type": "application/json"}
        payload = {
            "enabled": True,
            "settings": {
                "channel_id": "20",
                "message": "Welcome {user.mention}",
                "unknown_setting": "discard me",
            },
        }
        response = await self.client.put(
            "/dashboard/api/guild/123456789012345678/module/welcome",
            headers=headers,
            data=json.dumps(payload),
        )
        self.assertEqual(response.status, 200, await response.text())
        result = await response.json()
        self.assertTrue(result["enabled"])
        self.assertEqual(result["settings"]["channel_id"], "20")
        self.assertNotIn("unknown_setting", result["settings"])

        stored = db.module_config("guild:123456789012345678", "welcome")
        self.assertTrue(stored["enabled"])
        self.assertEqual(stored["settings"]["channel_id"], "20")
        self.assertEqual(len(db.dashboard_audit_list("123456789012345678")), 1)

        rejected = await self.client.put(
            "/dashboard/api/guild/123456789012345678/module/welcome",
            headers={"Cookie": cookie, "Content-Type": "application/json"},
            data=json.dumps(payload),
        )
        self.assertEqual(rejected.status, 403)

    async def test_booster_workspace_exposes_records_settings_and_actions(self):
        cookie, csrf = await self._login()
        auth_headers = {"Cookie": cookie}
        write_headers = {
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
            "Content-Type": "application/json",
        }
        db.booster_record_event("123456789012345678", "40", "boost-message-1")

        page = await self.client.get("/dashboard", headers=auth_headers)
        page_body = await page.text()
        self.assertIn('data-view="boosters"', page_body)
        self.assertIn("Save every Booster Perks setting", page_body)
        script = await (await self.client.get("/dashboard/assets/app.js")).text()
        self.assertIn("boosterGroups", script)
        self.assertIn("collectionSchemas", script)
        self.assertIn("boost_level_roles", script)
        self.assertIn("emoji_restrictions", script)
        self.assertIn("stat_channels", script)

        response = await self.client.get(
            "/dashboard/api/guild/123456789012345678/boosters",
            headers=auth_headers,
        )
        self.assertEqual(response.status, 200, await response.text())
        payload = await response.json()
        self.assertEqual(payload["stats"]["current_boosts"], 1)
        self.assertEqual(payload["records"][0]["name"], "Booster One")
        self.assertEqual({item["id"] for item in payload["members"]}, {"40", "41"})

        denied = await self.client.post(
            "/dashboard/api/guild/123456789012345678/boosters/action",
            headers={"Cookie": cookie, "Content-Type": "application/json"},
            json={"action": "adjust", "target_id": "40", "delta": 2},
        )
        self.assertEqual(denied.status, 403)

        adjusted = await self.client.post(
            "/dashboard/api/guild/123456789012345678/boosters/action",
            headers=write_headers,
            json={"action": "adjust", "target_id": "40", "delta": 2},
        )
        self.assertEqual(adjusted.status, 200, await adjusted.text())
        self.assertEqual((await adjusted.json())["record"]["current_boosts"], 3)

        queued = await self.client.post(
            "/dashboard/api/guild/123456789012345678/boosters/action",
            headers=write_headers,
            json={"action": "test", "target_id": "40"},
        )
        self.assertEqual(queued.status, 202, await queued.text())
        actions = db.community_records(
            "booster_dashboard_action", "guild:123456789012345678", limit=10
        )
        self.assertEqual({item["data"]["action"] for item in actions}, {"reconcile", "test"})
        audit = db.dashboard_audit_list("123456789012345678", 20)
        self.assertIn("booster.adjusted", {item["action"] for item in audit})
        self.assertIn("booster.test.queued", {item["action"] for item in audit})

    async def test_booster_actions_validate_member_and_action(self):
        cookie, csrf = await self._login()
        headers = {
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
            "Content-Type": "application/json",
        }
        invalid_member = await self.client.post(
            "/dashboard/api/guild/123456789012345678/boosters/action",
            headers=headers,
            json={"action": "adjust", "target_id": "999", "delta": 1},
        )
        self.assertEqual(invalid_member.status, 400)
        invalid_action = await self.client.post(
            "/dashboard/api/guild/123456789012345678/boosters/action",
            headers=headers,
            json={"action": "delete_everything"},
        )
        self.assertEqual(invalid_action.status, 400)

    async def test_dedicated_booster_page_covers_every_catalog_setting_once(self):
        group_bodies = re.findall(r"keys:\[([^\]]*)\]", _JS)
        grouped_keys = [
            key
            for body in group_bodies
            for key in re.findall(r'"([a-z0-9_]+)"', body)
        ]
        self.assertEqual(set(grouped_keys), set(default_settings("boosters")))
        self.assertEqual(len(grouped_keys), len(set(grouped_keys)))

    async def test_catalog_exposes_every_module_without_paid_gates(self):
        cookie, _csrf = await self._login()
        response = await self.client.get(
            "/dashboard/api/catalog", headers={"Cookie": cookie}
        )
        payload = await response.json()
        self.assertTrue(payload["free"])
        self.assertEqual({item["id"] for item in payload["modules"]}, set(MODULES))
        self.assertEqual(
            {item["key"] for item in payload["server_settings"]}, set(SERVER_SETTINGS)
        )
        model = next(
            item for item in payload["server_settings"] if item["key"] == "model"
        )
        self.assertTrue(any(choice["value"] == "" for choice in model["choices"]))
        self.assertGreaterEqual(len(payload["modules"]), 32)

    async def test_server_settings_round_trip_validation_csrf_and_audit(self):
        cookie, csrf = await self._login()
        headers = {
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
            "Content-Type": "application/json",
        }
        payload = {
            "settings": {
                "persona": "  dashboard persona  ",
                "model": "openai/gpt-oss-20b",
                "language": "hu",
                "smart_always": False,
                "allowed_channels": ["20", "20", "not-an-id"],
                "lurk": True,
                "lurk_channel": "20",
                "history_enabled": True,
                "retention_days": 999,
                "moderation_enabled": True,
                "modlog_channel": "not-an-id",
                "rules_enabled": True,
                "approval_channel": "20",
                "voice_transcription_enabled": False,
                "unknown": "discard me",
            }
        }
        response = await self.client.put(
            "/dashboard/api/guild/123456789012345678/settings",
            headers=headers,
            data=json.dumps(payload),
        )
        self.assertEqual(response.status, 200, await response.text())
        settings = (await response.json())["settings"]
        self.assertEqual(settings["persona"], "dashboard persona")
        self.assertFalse(settings["smart_always"])
        self.assertEqual(settings["allowed_channels"], ["20"])
        self.assertEqual(settings["retention_days"], 30)
        self.assertEqual(settings["modlog_channel"], "")
        self.assertNotIn("unknown", settings)

        stored = db.guild_settings("guild:123456789012345678")
        self.assertEqual(stored, settings)
        audit = db.dashboard_audit_list("123456789012345678")
        self.assertEqual(audit[0]["action"], "settings.updated")
        self.assertEqual(audit[0]["module"], "server_settings")
        self.assertNotIn("dashboard persona", json.dumps(audit[0]))

        rejected = await self.client.put(
            "/dashboard/api/guild/123456789012345678/settings",
            headers={"Cookie": cookie, "Content-Type": "application/json"},
            data=json.dumps(payload),
        )
        self.assertEqual(rejected.status, 403)

        invalid_model = await self.client.put(
            "/dashboard/api/guild/123456789012345678/settings",
            headers=headers,
            data=json.dumps({"settings": {**settings, "model": "attacker/model"}}),
        )
        self.assertEqual(invalid_model.status, 400)

    async def test_public_form_validates_and_persists_submission(self):
        db.module_config_set(
            "123456789012345678",
            "forms",
            enabled=True,
            actor_id="test",
            settings={
                "public_base_url": "https://example.test",
                "forms": [
                    {
                        "slug": "staff",
                        "title": "Staff <Application>",
                        "description": "Apply & help",
                        "enabled": True,
                        "channel_id": "20",
                        "questions": [
                            {"id": "why", "label": "Why?", "type": "paragraph", "required": True}
                        ],
                    }
                ],
            },
        )
        page = await self.client.get("/forms/123456789012345678/staff")
        self.assertEqual(page.status, 200)
        body = await page.text()
        self.assertIn("Staff &lt;Application&gt;", body)
        self.assertNotIn("<Application>", body)
        self.assertIn("style-src 'self'", page.headers["Content-Security-Policy"])

        missing = await self.client.post(
            "/forms/123456789012345678/staff", data={}
        )
        self.assertEqual(missing.status, 400)
        submitted = await self.client.post(
            "/forms/123456789012345678/staff", data={"q_why": "I can help."}
        )
        self.assertEqual(submitted.status, 200)
        records = db.community_records(
            "form_submission", "guild:123456789012345678"
        )
        self.assertEqual(records[0]["data"]["answers"][0]["values"], ["I can help."])

    async def test_staff_operations_api_is_authenticated_csrf_bound_and_export_safe(self):
        cookie, csrf = await self._login()
        headers = {
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
            "Content-Type": "application/json",
        }
        created = await self.client.post(
            "/dashboard/api/guild/123456789012345678/cases",
            headers=headers,
            data=json.dumps({
                "subject_id": "40",
                "category": "automod bypass",
                "reason": "Repeated separator bypass",
                "severity": "high",
                "evidence_links": ["https://discord.com/channels/123/20/30"],
            }),
        )
        self.assertEqual(created.status, 201, await created.text())
        case = (await created.json())["case"]
        operations = await self.client.get(
            "/dashboard/api/guild/123456789012345678/operations?q=separator",
            headers={"Cookie": cookie},
        )
        self.assertEqual(operations.status, 200, await operations.text())
        payload = await operations.json()
        self.assertEqual(case["id"], payload["cases"][0]["id"])
        self.assertTrue(payload["health"]["advisory_only"])
        self.assertTrue(payload["retention"]["modules"])

        note = await self.client.post(
            f"/dashboard/api/guild/123456789012345678/case/{case['id']}/action",
            headers=headers,
            data=json.dumps({"action": "note", "note": "Private staff context"}),
        )
        self.assertEqual(note.status, 200, await note.text())
        self.assertTrue(any(item["kind"] == "note" for item in (await note.json())["case"]["timeline"]))

        no_csrf = await self.client.post(
            "/dashboard/api/guild/123456789012345678/cases",
            headers={"Cookie": cookie, "Content-Type": "application/json"},
            data="{}",
        )
        self.assertEqual(no_csrf.status, 403)
        exported = await self.client.get(
            "/dashboard/api/guild/123456789012345678/analytics.csv",
            headers={"Cookie": cookie},
        )
        self.assertEqual(exported.status, 200)
        csv_body = await exported.text()
        self.assertIn("moderation_case,open,1", csv_body)
        self.assertNotIn("Repeated separator bypass", csv_body)


class ModuleCatalogTests(unittest.TestCase):
    def test_merge_settings_keeps_known_compatible_values_only(self):
        merged = merge_settings(
            "automod",
            {
                "delete": False,
                "max_mentions": 12,
                "banned_phrases": ["one", "two"],
                "log_channel_id": 123,
                "not_real": True,
            },
        )
        self.assertFalse(merged["delete"])
        self.assertEqual(merged["max_mentions"], 12)
        self.assertEqual(merged["banned_phrases"], ["one", "two"])
        self.assertEqual(merged["log_channel_id"], "")
        self.assertNotIn("not_real", merged)

    def test_module_ids_and_prefix_receive_field_level_validation(self):
        merged = merge_settings(
            "bot_controls",
            {
                "prefix": " too long prefix ",
                "allowed_channel_ids": ["10", "10", "nope", 20],
                "allowed_role_ids": ["30", "not-a-role"],
            },
        )
        self.assertEqual(merged["prefix"], "")
        self.assertEqual(merged["allowed_channel_ids"], ["10", "20"])
        self.assertEqual(merged["allowed_role_ids"], ["30"])


class EconomyAtomicityTests(unittest.TestCase):
    def setUp(self):
        db.close()
        self.previous_db = config.DB_PATH
        config.DB_PATH = ":memory:"
        db.conn()

    def tearDown(self):
        db.close()
        config.DB_PATH = self.previous_db

    def test_spend_and_transfer_are_atomic(self):
        db.economy_adjust("one", 200)
        self.assertEqual(db.economy_spend("one", 50), 150)
        self.assertEqual(db.economy_transfer("one", "two", 40), (110, 40))
        with self.assertRaisesRegex(ValueError, "enough"):
            db.economy_transfer("two", "one", 100)
        self.assertEqual(db.economy_profile("one")["balance"], 110)
        self.assertEqual(db.economy_profile("two")["balance"], 40)


if __name__ == "__main__":
    unittest.main()
