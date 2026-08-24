from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from sefbot.legal import LEGAL_EFFECTIVE_DATE, LEGAL_VERSION
from sefbot.web import (
    ReadinessState,
    WebConfigurationError,
    WebService,
    _environment_port,
    create_app,
)


class WebApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = ReadinessState()
        app = create_app(
            privacy_contact="privacy@example.test", readiness=self.state
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_legal_pages_are_html_and_hardened(self) -> None:
        for path in ("/sefbot", "/sefbot/terms", "/sefbot/privacy"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.content_type, "text/html")
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                self.assertEqual(
                    response.headers["X-Content-Type-Options"], "nosniff"
                )
                self.assertEqual(response.headers["Server"], "OpSef")
                self.assertIn(
                    "max-age=31536000",
                    response.headers["Strict-Transport-Security"],
                )
                self.assertIn(
                    "frame-ancestors 'none'",
                    response.headers["Content-Security-Policy"],
                )
                self.assertEqual(
                    response.headers["Cache-Control"], "public, max-age=300"
                )
                body = await response.text()
                self.assertIn("OpSef", body)
                if path != "/sefbot":
                    self.assertIn(f"Version {LEGAL_VERSION}", body)
                    self.assertIn(f"effective {LEGAL_EFFECTIVE_DATE}", body)

    async def test_legal_pages_match_the_running_privacy_model(self) -> None:
        terms = await (await self.client.get("/sefbot/terms")).text()
        privacy = await (await self.client.get("/sefbot/privacy")).text()
        self.assertIn("/tos accept", terms)
        self.assertIn("consent to store raw message", terms)
        self.assertIn("SEFBOT_OWNER_ID", terms)
        self.assertIn("TOS_STRIKE_LIMIT = 3", terms)
        self.assertIn("Daki Hosting", terms)
        self.assertIn("privacy_consents", privacy)
        self.assertIn("action_audit", privacy)
        self.assertIn("does not erase", privacy)
        self.assertIn("30 days", privacy)
        self.assertIn("STT", privacy)
        self.assertIn("Groq", privacy)
        self.assertNotIn("<script>alert", privacy)

    async def test_health_does_not_claim_dependency_readiness(self) -> None:
        health = await self.client.get("/healthz")
        self.assertEqual(health.status, 200)
        self.assertEqual(await health.json(), {"status": "ok"})
        self.assertEqual(health.headers["Cache-Control"], "no-store")
        self.assertEqual(health.headers["X-Robots-Tag"], "noindex, nofollow")

        not_ready = await self.client.get("/readyz")
        self.assertEqual(not_ready.status, 503)
        self.assertEqual(
            await not_ready.json(),
            {"status": "not_ready", "discord": False, "database": False},
        )

        self.state.discord = True
        self.state.database = True
        ready = await self.client.get("/readyz")
        self.assertEqual(ready.status, 200)
        self.assertEqual(
            await ready.json(),
            {"status": "ready", "discord": True, "database": True},
        )

    async def test_compatibility_routes_redirect_without_reflecting_input(self) -> None:
        cases = {
            "/sefbot/": "/sefbot",
            "/sefbot/tos": "/sefbot/terms",
            "/opsef-tos.html": "/sefbot/terms",
            "/opsef-privacy.html": "/sefbot/privacy",
        }
        for path, location in cases.items():
            with self.subTest(path=path):
                response = await self.client.get(path, allow_redirects=False)
                self.assertEqual(response.status, 308)
                self.assertEqual(response.headers["Location"], location)
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    async def test_unsupported_methods_and_unknown_routes_are_safe(self) -> None:
        method_response = await self.client.post("/sefbot", data=b"ignored")
        self.assertEqual(method_response.status, 405)
        self.assertEqual(method_response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(method_response.headers["Cache-Control"], "no-store")

        missing_response = await self.client.get("/not-a-route")
        self.assertEqual(missing_response.status, 404)
        self.assertEqual(missing_response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(missing_response.headers["Cache-Control"], "no-store")

    async def test_contact_is_escaped(self) -> None:
        client = TestClient(
            TestServer(
                create_app(
                    privacy_contact='<script>alert("x")</script>',
                    readiness=lambda: True,
                )
            )
        )
        await client.start_server()
        try:
            response = await client.get("/sefbot/privacy")
            body = await response.text()
            self.assertNotIn('<script>alert("x")</script>', body)
            self.assertIn("&lt;script&gt;", body)
        finally:
            await client.close()

    async def test_readiness_failure_is_sanitized(self) -> None:
        async def broken_readiness() -> bool:
            raise RuntimeError("database-password-should-not-leak")

        client = TestClient(
            TestServer(
                create_app(
                    privacy_contact="privacy@example.test",
                    readiness=broken_readiness,
                )
            )
        )
        await client.start_server()
        try:
            with self.assertLogs("sefbot.web", level="WARNING"):
                response = await client.get("/readyz")
            self.assertEqual(response.status, 503)
            body = await response.text()
            self.assertNotIn("database-password", body)
            self.assertEqual(await response.json(), {"status": "not_ready"})
        finally:
            await client.close()

    async def test_readiness_rejects_unexpected_component_names(self) -> None:
        client = TestClient(
            TestServer(
                create_app(
                    privacy_contact="privacy@example.test",
                    readiness=lambda: {"provider_password": True},
                )
            )
        )
        await client.start_server()
        try:
            with self.assertLogs("sefbot.web", level="WARNING"):
                response = await client.get("/readyz")
            self.assertEqual(response.status, 503)
            body = await response.text()
            self.assertNotIn("provider_password", body)
            self.assertEqual(await response.json(), {"status": "not_ready"})
        finally:
            await client.close()

    async def test_web_service_lifecycle_is_idempotent(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        service = WebService(
            privacy_contact="privacy@example.test",
            readiness=lambda: True,
            host="127.0.0.1",
            port=port,
        )
        await service.start()
        await service.start()
        await service.close()
        await service.close()


class WebConfigurationTests(unittest.TestCase):
    def test_privacy_contact_is_required(self) -> None:
        for contact in ("", "privacy@example.test\nspoofed", None):
            with self.subTest(contact=contact):
                with self.assertRaises(WebConfigurationError):
                    create_app(privacy_contact=contact)  # type: ignore[arg-type]

    def test_readiness_provider_must_be_callable(self) -> None:
        with self.assertRaises(WebConfigurationError):
            create_app(
                privacy_contact="privacy@example.test",
                readiness=True,  # type: ignore[arg-type]
            )

    def test_listener_address_is_validated(self) -> None:
        for host, port in (("host\nname", 8080), ("127.0.0.1", 0), ("localhost", "x")):
            with self.subTest(host=host, port=port):
                with self.assertRaises(WebConfigurationError):
                    WebService(
                        privacy_contact="privacy@example.test",
                        readiness=lambda: True,
                        host=host,
                        port=port,
                    )

    def test_daki_server_port_is_used_when_railway_port_is_unset(self) -> None:
        with mock.patch.dict(os.environ, {"SERVER_PORT": "4204"}, clear=False):
            os.environ.pop("PORT", None)
            self.assertEqual(_environment_port(), 4204)


class PublicSiteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        kozzyx = root / "kozzyx"
        kozzyx.mkdir()
        (kozzyx / "index.html").write_text("<html>kozzyx-home</html>", encoding="utf-8")
        (kozzyx / "secret.env").write_text("token=nope", encoding="utf-8")
        (kozzyx / "about.html").write_text("<html>about</html>", encoding="utf-8")
        kirmy = root / "kirmy"
        kirmy.mkdir()
        (kirmy / "index.html").write_text("<html>kirmy-home</html>", encoding="utf-8")
        wearegays = root / "wearegays"
        (wearegays / "pages").mkdir(parents=True)
        (wearegays / "index.html").write_text("<html>wag-home</html>", encoding="utf-8")
        (wearegays / "nano-terms.html").write_text("<html>terms</html>", encoding="utf-8")
        (wearegays / "pages" / "wearegays.html").write_text(
            "<html>pride</html>", encoding="utf-8"
        )
        app = create_app(
            privacy_contact="privacy@example.test",
            readiness=ReadinessState(),
            sites_root=root,
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.tmpdir.cleanup()

    async def test_host_header_selects_site_and_skips_opsef_csp(self) -> None:
        response = await self.client.get("/", headers={"Host": "kirmy.org"})
        self.assertEqual(response.status, 200)
        self.assertIn("kirmy-home", await response.text())
        self.assertNotIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    async def test_forwarded_host_selects_site_behind_a_proxy(self) -> None:
        response = await self.client.get(
            "/",
            headers={
                "Host": "paid5.daki.cc:4204",
                "X-Forwarded-Host": "wearegays.net",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertIn("wag-home", await response.text())

    async def test_legal_routes_stay_hardened_on_public_hosts(self) -> None:
        response = await self.client.get("/sefbot", headers={"Host": "kozzyx.org"})
        self.assertEqual(response.status, 200)
        self.assertIn("OpSef", await response.text())
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])

    async def test_dotfiles_and_traversal_are_rejected(self) -> None:
        blocked = await self.client.get("/secret.env", headers={"Host": "kozzyx.org"})
        self.assertEqual(blocked.status, 404)
        self.assertNotIn("token=nope", await blocked.text())
        traversal = await self.client.get(
            "/../../secret.env", headers={"Host": "kozzyx.org"}
        )
        self.assertEqual(traversal.status, 404)

    async def test_html_extension_fallback_and_wearegays_redirects(self) -> None:
        about = await self.client.get("/about", headers={"Host": "kozzyx.org"})
        self.assertEqual(about.status, 200)
        self.assertIn("about", await about.text())
        redirected = await self.client.get(
            "/tos", headers={"Host": "wearegays.net"}, allow_redirects=False
        )
        self.assertEqual(redirected.status, 301)
        self.assertEqual(redirected.headers["Location"], "/nano-terms.html")
        pride = await self.client.get(
            "/wearegays", headers={"Host": "www.wearegays.net"}, allow_redirects=False
        )
        self.assertEqual(pride.status, 301)
        self.assertEqual(pride.headers["Location"], "/pages/wearegays.html")


if __name__ == "__main__":
    unittest.main()
