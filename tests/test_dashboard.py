import json
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from aiohttp.test_utils import TestClient, TestServer

from sefbot import config, db
from sefbot.dashboard import DashboardAuthConfig
from sefbot.module_catalog import MODULES, merge_settings
from sefbot.web import create_app


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        db.close()
        self.previous_db = config.DB_PATH
        config.DB_PATH = ":memory:"
        self.app = create_app(
            privacy_contact="privacy@example.test",
            dashboard_token="a-long-private-dashboard-token",
            guild_provider=lambda: [
                {
                    "id": "123456789012345678",
                    "name": "Test Server",
                    "member_count": 42,
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
        response = await self.client.post(
            "/dashboard/login",
            data={"token": "a-long-private-dashboard-token"},
            allow_redirects=False,
        )
        self.assertEqual(response.status, 303)
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
        self.assertIn("Create your account", body)
        self.assertIn("Continue with Google", body)
        self.assertIn("Continue with Microsoft", body)
        self.assertIn("Continue with Apple", body)
        self.assertIn("Continue with email", body)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

        api = await self.client.get("/dashboard/api/catalog")
        self.assertEqual(api.status, 401)

    async def test_account_then_discord_oauth_limits_visible_guilds(self):
        auth = DashboardAuthConfig(
            public_url="https://wearegays.net",
            session_secret="s" * 48,
            firebase_api_key="firebase-public-key",
            firebase_auth_domain="example.firebaseapp.com",
            firebase_project_id="example",
            firebase_app_id="1:123:web:abc",
            discord_client_id="123456789012345678",
            discord_client_secret="discord-secret",
        )
        app = create_app(
            privacy_contact="privacy@example.test",
            dashboard_auth=auth,
            guild_provider=lambda: [
                {"id": "123456789012345678", "name": "Managed"},
                {"id": "987654321098765432", "name": "Not managed"},
            ],
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)

        config_response = await client.get("/dashboard/api/auth/config")
        self.assertEqual(config_response.status, 200)
        auth_setup = await config_response.json()
        with mock.patch(
            "sefbot.dashboard._firebase_user",
            new=mock.AsyncMock(
                return_value={
                    "localId": "firebase-user",
                    "emailVerified": True,
                    "providerUserInfo": [{"providerId": "google.com"}],
                }
            ),
        ):
            response = await client.post(
                "/dashboard/auth/firebase",
                headers={"X-Login-CSRF": auth_setup["csrf"]},
                json={"id_token": "x" * 200},
            )
        self.assertEqual(response.status, 200, await response.text())
        connect = await client.get("/dashboard")
        self.assertIn("Connect Discord", await connect.text())
        self.assertEqual((await client.get("/dashboard/api/guilds")).status, 401)

        start = await client.get("/dashboard/auth/discord", allow_redirects=False)
        self.assertEqual(start.status, 303)
        state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
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
                allow_redirects=False,
            )
        self.assertEqual(callback.status, 303, await callback.text())
        guilds = await (await client.get("/dashboard/api/guilds")).json()
        self.assertEqual(
            [item["id"] for item in guilds["guilds"]], ["123456789012345678"]
        )

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

    async def test_catalog_exposes_every_module_without_paid_gates(self):
        cookie, _csrf = await self._login()
        response = await self.client.get(
            "/dashboard/api/catalog", headers={"Cookie": cookie}
        )
        payload = await response.json()
        self.assertTrue(payload["free"])
        self.assertEqual({item["id"] for item in payload["modules"]}, set(MODULES))
        self.assertGreaterEqual(len(payload["modules"]), 32)

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
