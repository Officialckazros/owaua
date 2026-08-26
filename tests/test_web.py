from __future__ import annotations

import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from aiohttp.test_utils import TestClient, TestServer

from sefbot import blocked, config, db, tos
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
        db.close()
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        self.old_acceptance_secret = config.TOS_ACCEPTANCE_SECRET
        self.old_proxy_secret = config.TOS_PROXY_SECRET
        config.DB_PATH = str(Path(self.tempdir.name) / "web.sqlite3")
        config.TOS_ACCEPTANCE_SECRET = "a" * 64
        config.TOS_PROXY_SECRET = "p" * 64
        self.state = ReadinessState()
        app = create_app(
            privacy_contact="privacy@example.test", readiness=self.state
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        db.close()
        config.DB_PATH = self.old_db_path
        config.TOS_ACCEPTANCE_SECRET = self.old_acceptance_secret
        config.TOS_PROXY_SECRET = self.old_proxy_secret
        self.tempdir.cleanup()

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
        self.assertIn("typed <code>tos accept</code> command no longer", terms)
        self.assertIn("keyed network token", terms)
        self.assertIn("regardless of Discord account age", terms)
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

    async def test_web_acceptance_is_single_use_and_never_stores_raw_ip(self) -> None:
        user_id = "175928847299117063"
        url = tos.issue_acceptance_url(user_id)
        token = parse_qs(urlparse(url).query)["token"][0]

        page = await self.client.get(f"/sefbot/terms/accept?token={token}")
        self.assertEqual(page.status, 200)
        self.assertIn("form-action 'self'", page.headers["Content-Security-Policy"])
        body = await page.text()
        self.assertIn("client IP address", body)
        self.assertIn("keyed network token", body)
        self.assertNotIn(user_id, body)

        response = await self.client.post(
            "/sefbot/terms/accept",
            data={"token": token, "agree": "yes"},
            headers={
                "X-SefBot-Origin-Auth": config.TOS_PROXY_SECRET,
                "X-Forwarded-For": "203.0.113.42",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertIn("Terms accepted", await response.text())
        self.assertTrue(tos.has_accepted(user_id))
        database_text = "\n".join(db.conn().iterdump())
        self.assertNotIn("203.0.113.42", database_text)

        replay = await self.client.post(
            "/sefbot/terms/accept",
            data={"token": token, "agree": "yes"},
            headers={
                "X-SefBot-Origin-Auth": config.TOS_PROXY_SECRET,
                "X-Forwarded-For": "203.0.113.42",
            },
        )
        self.assertIn("already used or expired", await replay.text())

    async def test_blocked_network_requires_review_for_established_account(self) -> None:
        address = "198.51.100.9"
        blocked_user = "175928847299117063"
        fingerprint = tos.network_fingerprint(address)
        db.tos_acceptance_set(
            blocked_user,
            tos.TOS_VERSION,
            status="accepted",
            network_hash=fingerprint,
        )
        blocked.block_user(
            blocked_user,
            reason="tos: test block",
            block_source="tos",
        )
        old_created_ms = int((time.time() - 500 * 86_400) * 1000)
        established_user = str((old_created_ms - 1_420_070_400_000) << 22)
        url = tos.issue_acceptance_url(established_user)
        token = parse_qs(urlparse(url).query)["token"][0]

        response = await self.client.post(
            "/sefbot/terms/accept",
            data={"token": token, "agree": "yes"},
            headers={
                "X-SefBot-Origin-Auth": config.TOS_PROXY_SECRET,
                "X-Forwarded-For": address,
            },
        )
        self.assertEqual(response.status, 200)
        self.assertIn("submitted for review", await response.text())
        self.assertFalse(tos.has_accepted(established_user))
        record = db.tos_acceptance_get(established_user)
        self.assertEqual("review", record["status"])
        self.assertEqual("blocked_network_match", record["risk_code"])

    async def test_blocking_account_reviews_preaccepted_network_peer(self) -> None:
        address = "198.51.100.44"
        blocked_user = "175928847299117063"
        peer_user = "275928847299117063"
        other_user = "375928847299117063"

        self.assertEqual("accepted", tos.record_web_acceptance(blocked_user, address))
        self.assertEqual("accepted", tos.record_web_acceptance(peer_user, address))
        self.assertEqual(
            "accepted", tos.record_web_acceptance(other_user, "198.51.100.45")
        )

        blocked.block_user(
            blocked_user,
            reason="tos: test block",
            block_source="tos",
        )

        peer_record = db.tos_acceptance_get(peer_user)
        self.assertEqual("review", peer_record["status"])
        self.assertEqual("blocked_network_match", peer_record["risk_code"])
        self.assertFalse(tos.has_accepted(peer_user))
        self.assertTrue(tos.has_accepted(other_user))

        self.assertTrue(tos.allow_review(peer_user))
        self.assertTrue(tos.has_accepted(peer_user))

    async def test_blocked_match_cannot_hide_beyond_bounded_user_lookup(self) -> None:
        address = "198.51.100.77"
        fingerprint = tos.network_fingerprint(address)
        blocked_user = "175928847299117063"
        db.tos_acceptance_set(
            blocked_user,
            tos.TOS_VERSION,
            status="accepted",
            network_hash=fingerprint,
            submitted_at=time.time() - 1_000,
        )
        blocked.block_user(blocked_user, reason="tos: test block", block_source="tos")
        for offset in range(101):
            db.tos_acceptance_set(
                str(275928847299117063 + offset),
                tos.TOS_VERSION,
                status="accepted",
                network_hash=fingerprint,
                submitted_at=time.time() - offset,
            )

        candidate = "475928847299117063"
        self.assertEqual("review", tos.record_web_acceptance(candidate, address))
        self.assertFalse(tos.has_accepted(candidate))

    async def test_untrusted_forwarded_address_fails_closed(self) -> None:
        user_id = "175928847299117063"
        token = parse_qs(urlparse(tos.issue_acceptance_url(user_id)).query)["token"][0]
        response = await self.client.post(
            "/sefbot/terms/accept",
            data={"token": token, "agree": "yes"},
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
        self.assertEqual(response.status, 503)
        self.assertFalse(tos.has_accepted(user_id))

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
            {
                "status": "not_ready",
                "discord": False,
                "database": False,
                "malware_scanner": False,
            },
        )

        self.state.discord = True
        self.state.database = True
        self.state.malware_scanner = True
        ready = await self.client.get("/readyz")
        self.assertEqual(ready.status, 200)
        self.assertEqual(
            await ready.json(),
            {
                "status": "ready",
                "discord": True,
                "database": True,
                "malware_scanner": True,
            },
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
        femsec = root / "femsec"
        (femsec / "boxes" / "exhibits").mkdir(parents=True)
        (femsec / "index.html").write_text("<html>opsef-files</html>", encoding="utf-8")
        (femsec / "boxes" / "exhibits" / "index.html").write_text(
            "<html>exhibits-box</html>", encoding="utf-8"
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

    async def test_femsec_host_serves_the_files_tree(self) -> None:
        home = await self.client.get("/", headers={"Host": "femsec.wearegays.net"})
        self.assertEqual(home.status, 200)
        self.assertIn("opsef-files", await home.text())
        exhibits = await self.client.get(
            "/boxes/exhibits/", headers={"Host": "femsec.wearegays.net"}
        )
        self.assertEqual(exhibits.status, 200)
        self.assertIn("exhibits-box", await exhibits.text())

    async def test_wearegays_dump_paths_redirect_to_femsec_host(self) -> None:
        exhibits = await self.client.get(
            "/boxes/exhibits/",
            headers={"Host": "wearegays.net"},
            allow_redirects=False,
        )
        self.assertEqual(exhibits.status, 301)
        self.assertEqual(
            exhibits.headers["Location"],
            "https://femsec.wearegays.net/boxes/exhibits/",
        )
        nested = await self.client.get(
            "/femsec/boxes/exhibits/",
            headers={"Host": "www.wearegays.net"},
            allow_redirects=False,
        )
        self.assertEqual(nested.status, 301)
        self.assertEqual(
            nested.headers["Location"],
            "https://femsec.wearegays.net/boxes/exhibits/",
        )


if __name__ == "__main__":
    unittest.main()
