from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from ask import ask
from cloudflare import (
    cloudflare_unreachable,
    describe_protection,
    gateway_base,
    log_endpoint,
    provider_urls,
    request_headers,
    ship_audit_record,
)
from memory import MemoryStore
from test_ask import FakeHTTP, model_reply


ACCOUNT = "a" * 32
GATEWAY_ENV = {
    "CLOUDFLARE_ACCOUNT_ID": ACCOUNT,
    "CLOUDFLARE_AI_GATEWAY": "owaua",
    "CLOUDFLARE_AI_GATEWAY_TOKEN": "gateway-token",
}


class CloudflareHelperTests(unittest.TestCase):
    def test_invalid_ids_keep_the_bot_on_direct_providers(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CLOUDFLARE_ACCOUNT_ID": "nope",
                "CLOUDFLARE_AI_GATEWAY": "owaua",
                "CLOUDFLARE_LOG_URL": "",
                "CLOUDFLARE_LOG_TOKEN": "",
            },
            clear=False,
        ):
            self.assertIsNone(gateway_base("perplexity"))
            self.assertEqual(describe_protection(), "off")

    def test_gateway_urls_match_provider_paths(self) -> None:
        with patch.dict(os.environ, GATEWAY_ENV, clear=False):
            self.assertEqual(
                gateway_base("perplexity"),
                f"https://gateway.ai.cloudflare.com/v1/{ACCOUNT}/owaua/compat",
            )
            primary, fallback = provider_urls(
                "perplexity", "https://api.perplexity.ai/v1"
            )
            self.assertEqual(primary, "https://api.perplexity.ai/v1/responses")
            self.assertIsNone(fallback)

    def test_full_mode_never_uses_the_gateway(self) -> None:
        with patch.dict(os.environ, GATEWAY_ENV, clear=False):
            primary, fallback = provider_urls(
                "perplexity", "https://api.perplexity.ai/v1", full_mode=True
            )
            self.assertEqual(primary, "https://api.perplexity.ai/v1/responses")
            self.assertIsNone(fallback)

    def test_skip_cache_header_is_always_set_for_gateway_requests(self) -> None:
        with patch.dict(os.environ, GATEWAY_ENV, clear=False):
            headers = request_headers(provider="perplexity", user_id="7", server_id="9")
        self.assertEqual(headers["cf-aig-skip-cache"], "true")
        self.assertEqual(headers["cf-aig-authorization"], "Bearer gateway-token")
        metadata = json.loads(headers["cf-aig-metadata"])
        self.assertEqual(metadata["provider"], "perplexity")
        self.assertNotEqual(metadata["user"], "7")
        self.assertNotEqual(metadata["server"], "9")
        self.assertEqual(len(metadata["user"]), 16)

    def test_only_edge_failures_are_retried(self) -> None:
        self.assertTrue(cloudflare_unreachable(httpx.ConnectError("nope")))
        self.assertTrue(cloudflare_unreachable(httpx.ConnectTimeout("nope")))
        self.assertFalse(cloudflare_unreachable(httpx.ReadTimeout("nope")))
        self.assertTrue(
            cloudflare_unreachable(
                httpx.HTTPStatusError(
                    "missing",
                    request=httpx.Request("POST", "https://gateway.ai.cloudflare.com"),
                    response=httpx.Response(404),
                )
            )
        )
        self.assertFalse(
            cloudflare_unreachable(
                httpx.HTTPStatusError(
                    "paid",
                    request=httpx.Request("POST", "https://gateway.ai.cloudflare.com"),
                    response=httpx.Response(500),
                )
            )
        )
        self.assertFalse(
            cloudflare_unreachable(
                httpx.HTTPStatusError(
                    "limited",
                    request=httpx.Request("POST", "https://gateway.ai.cloudflare.com"),
                    response=httpx.Response(429),
                )
            )
        )

    def test_log_url_rejects_non_https_and_unknown_hosts(self) -> None:
        with patch.dict(os.environ, {"CLOUDFLARE_LOG_URL": "http://owaua-audit.example.workers.dev/"}, clear=False):
            self.assertEqual(log_endpoint(), "")
        with patch.dict(os.environ, {"CLOUDFLARE_LOG_URL": "https://evil.example/steal"}, clear=False):
            self.assertEqual(log_endpoint(), "")
        with patch.dict(
            os.environ,
            {"CLOUDFLARE_LOG_URL": "https://owaua-audit.ckazro.workers.dev/"},
            clear=False,
        ):
            self.assertEqual(log_endpoint(), "https://owaua-audit.ckazro.workers.dev/")


class CloudflareAskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(Path(self.temporary_directory.name) / "memory.sqlite3")
        self.http = FakeHTTP()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def _ask(self, **kwargs: object) -> str | None:
        defaults: dict[str, object] = {
            "event_id": "99",
            "scope_id": "123",
            "user_id": "7",
            "server_id": "",
            "prompt": "hello",
            "image_urls": [],
            "persona": "rudeish",
            "created_at": time.time(),
        }
        defaults.update(kwargs)
        return await ask(self.http, self.memory, **defaults)  # type: ignore[arg-type]

    async def test_hangout_uses_the_gateway_when_configured(self) -> None:
        with patch.dict(os.environ, GATEWAY_ENV, clear=False):
            answer = await self._ask()
        self.assertEqual(answer, "allowed reply")
        self.assertEqual(len(self.http.calls), 1)
        url, recorded = self.http.calls[0]
        self.assertIn("api.perplexity.ai", url)
        self.assertTrue(url.endswith("/responses"))
        self.assertNotIn("gateway.ai.cloudflare.com", url)
        self.assertNotIn("cf-aig-skip-cache", recorded["headers"])

    async def test_full_mode_stays_on_openai_directly(self) -> None:
        with patch.dict(os.environ, GATEWAY_ENV, clear=False):
            await self._ask(full_mode=True)
        url, recorded = self.http.calls[0]
        self.assertIn("api.openai.com", url)
        self.assertTrue(url.endswith("/v1/responses"))
        self.assertNotIn("gateway.ai.cloudflare.com", url)
        self.assertNotIn("cf-aig-skip-cache", recorded["headers"])

    async def test_missing_gateway_falls_back_to_the_same_provider(self) -> None:
        with patch.dict(os.environ, GATEWAY_ENV, clear=False):
            answer = await self._ask()
        self.assertEqual(answer, "allowed reply")
        self.assertEqual(len(self.http.calls), 1)
        self.assertIn("api.perplexity.ai", self.http.calls[0][0])
        self.assertNotIn("cf-aig-skip-cache", self.http.calls[0][1]["headers"])

    async def test_provider_errors_through_the_gateway_are_not_retried(self) -> None:
        self.http.responses = httpx.HTTPStatusError(
            "failed",
            request=httpx.Request("POST", "https://api.perplexity.ai/v1/responses"),
            response=httpx.Response(500),
        )
        with patch.dict(os.environ, GATEWAY_ENV, clear=False), self.assertRaises(RuntimeError):
            await self._ask()
        self.assertEqual(len(self.http.calls), 1)

    async def test_timeout_through_the_gateway_is_not_retried(self) -> None:
        self.http.responses = httpx.ReadTimeout("timed out")
        with patch.dict(os.environ, GATEWAY_ENV, clear=False), self.assertRaises(RuntimeError):
            await self._ask()
        self.assertEqual(len(self.http.calls), 1)

    async def test_unconfigured_cloudflare_keeps_direct_deepseek(self) -> None:
        with patch.dict(
            os.environ,
            {"CLOUDFLARE_ACCOUNT_ID": "", "CLOUDFLARE_AI_GATEWAY": ""},
            clear=False,
        ):
            await self._ask()
        self.assertIn("api.perplexity.ai", self.http.calls[0][0])


class CloudflareLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_shipping_never_raises_and_skips_bad_urls(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CLOUDFLARE_LOG_URL": "https://evil.example/steal",
                "CLOUDFLARE_LOG_TOKEN": "x" * 32,
            },
            clear=False,
        ):
            ship_audit_record('{"event":"music_command"}')
        self.assertIsNone(ship_audit_record("{not-used}"))
