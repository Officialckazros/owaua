import assert from "node:assert/strict";
import test from "node:test";

import worker from "../src/index.js";


const ORIGIN = "http://paid5.daki.cc:4204";

test("rejects unknown paths and methods without contacting upstream", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    throw new Error("should not be called");
  };
  try {
    const missing = await worker.fetch(new Request("https://wearegays.net/private"), {});
    assert.equal(missing.status, 404);
    assert.equal(missing.headers.get("X-Frame-Options"), "DENY");
    assert.equal(missing.headers.get("Cache-Control"), "no-store");

    const wrongHost = await worker.fetch(
      new Request("https://untrusted.example/sefbot"),
      {},
    );
    assert.equal(wrongHost.status, 404);

    const insecure = await worker.fetch(
      new Request("http://wearegays.net/sefbot"),
      {},
    );
    assert.equal(insecure.status, 400);

    const post = await worker.fetch(
      new Request("https://wearegays.net/sefbot", { method: "POST", body: "data" }),
      {},
    );
    assert.equal(post.status, 405);
    assert.equal(post.headers.get("Allow"), "GET, HEAD, POST");
    assert.equal(calls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("forwards only allowlisted metadata and drops query strings", async () => {
  const originalFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url: url.toString(), init };
    return new Response("<h1>Terms</h1>", {
      headers: {
        "Content-Type": "text/html",
        "Set-Cookie": "secret=value",
      },
    });
  };
  try {
    const request = new Request("https://wearegays.net/sefbot/terms?token=secret", {
      headers: {
        Authorization: "Bearer caller-secret",
        Cookie: "session=secret",
        "CF-Ray": "abc-123",
        "X-Forwarded-For": "127.0.0.1",
      },
    });
    const result = await worker.fetch(request, { SEFBOT_ORIGIN: ORIGIN });
    assert.equal(result.status, 200);
    assert.equal(captured.url, `${ORIGIN}/sefbot/terms`);
    assert.equal(captured.init.headers.get("Authorization"), null);
    assert.equal(captured.init.headers.get("Cookie"), null);
    assert.equal(captured.init.headers.get("X-Forwarded-For"), null);
    assert.equal(captured.init.headers.get("X-Request-ID"), "abc-123");
    assert.equal(result.headers.get("Set-Cookie"), null);
    assert.equal(result.headers.get("X-Content-Type-Options"), "nosniff");
    assert.equal(result.headers.get("Cache-Control"), "public, max-age=300");
    assert.match(result.headers.get("Content-Security-Policy"), /default-src 'none'/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("rewrites same-origin redirects and does not proxy an external location", async () => {
  const originalFetch = globalThis.fetch;
  let external = false;
  globalThis.fetch = async () =>
    new Response(null, {
      status: 308,
      headers: {
        Location: external ? "https://malicious.example/" : "/sefbot/terms",
      },
    });
  try {
    const request = new Request("https://wearegays.net/sefbot/tos");
    const local = await worker.fetch(request, { SEFBOT_ORIGIN: ORIGIN });
    assert.equal(local.headers.get("Location"), "https://wearegays.net/sefbot/terms");

    external = true;
    const blocked = await worker.fetch(request, { SEFBOT_ORIGIN: ORIGIN });
    assert.equal(blocked.headers.get("Location"), null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("rejects unsafe origins and sanitizes upstream failures", async () => {
  const badOrigin = await worker.fetch(new Request("https://wearegays.net/sefbot"), {
    SEFBOT_ORIGIN: "http://127.0.0.1:8080/path?secret=value",
  });
  assert.equal(badOrigin.status, 502);
  assert.equal(await badOrigin.text(), "Service unavailable");

  const untrustedOrigin = await worker.fetch(
      new Request("https://wearegays.net/sefbot"),
    { SEFBOT_ORIGIN: "https://attacker.example" },
  );
  assert.equal(untrustedOrigin.status, 502);

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response("database password: secret", { status: 500 });
  try {
    const result = await worker.fetch(
      new Request("https://wearegays.net/readyz"),
      { SEFBOT_ORIGIN: ORIGIN },
    );
    assert.equal(result.status, 502);
    assert.doesNotMatch(await result.text(), /database|secret/);
    assert.equal(result.headers.get("Cache-Control"), "no-store");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("HEAD responses never include an upstream body", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response("content", { headers: { "Content-Type": "text/html" } });
  try {
    const result = await worker.fetch(
      new Request("https://wearegays.net/sefbot", { method: "HEAD" }),
      { SEFBOT_ORIGIN: ORIGIN },
    );
    assert.equal(result.status, 200);
    assert.equal(await result.text(), "");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("preserves bounded readiness JSON and rejects response type confusion", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response('{"status":"not_ready","database":false}', {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  try {
    const request = new Request("https://wearegays.net/readyz");
    const pending = await worker.fetch(request, { SEFBOT_ORIGIN: ORIGIN });
    assert.equal(pending.status, 503);
    assert.deepEqual(await pending.json(), {
      status: "not_ready",
      database: false,
    });

    globalThis.fetch = async () =>
      new Response('{"status":"not_ready","database":false,"secret":"password"}', {
        status: 503,
        headers: { "Content-Type": "application/json" },
      });
    const leaked = await worker.fetch(request, { SEFBOT_ORIGIN: ORIGIN });
    assert.equal(leaked.status, 502);
    assert.doesNotMatch(await leaked.text(), /secret|password/);

    globalThis.fetch = async () =>
      new Response("<script>unexpected</script>", {
        headers: { "Content-Type": "text/html" },
      });
    const confused = await worker.fetch(request, { SEFBOT_ORIGIN: ORIGIN });
    assert.equal(confused.status, 502);
    assert.doesNotMatch(await confused.text(), /script|unexpected/);

    globalThis.fetch = async () =>
      new Response('{"status":"ready"}', {
        headers: {
          "Content-Type": "application/json",
          "Content-Length": "999999999",
        },
      });
    const oversized = await worker.fetch(request, { SEFBOT_ORIGIN: ORIGIN });
    assert.equal(oversized.status, 502);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("forwards only a validated acceptance token and the authenticated form POST", async () => {
  const originalFetch = globalThis.fetch;
  const token = "a".repeat(40);
  const captured = [];
  globalThis.fetch = async (url, init) => {
    captured.push({ url: url.toString(), init });
    return new Response("<h1>Terms</h1>", {
      headers: { "Content-Type": "text/html" },
    });
  };
  try {
    const get = await worker.fetch(
      new Request(`https://wearegays.net/sefbot/terms/accept?token=${token}`),
      { SEFBOT_ORIGIN: ORIGIN },
    );
    assert.equal(get.status, 200);
    assert.equal(captured[0].url, `${ORIGIN}/sefbot/terms/accept?token=${token}`);

    const post = await worker.fetch(
      new Request("https://wearegays.net/sefbot/terms/accept", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: `token=${token}&agree=yes`,
      }),
      { SEFBOT_ORIGIN: ORIGIN, ORIGIN_AUTH_SECRET: "s".repeat(32) },
    );
    assert.equal(post.status, 200);
    assert.equal(captured[1].init.headers.get("X-SefBot-Origin-Auth"), "s".repeat(32));
    assert.equal(await new Response(captured[1].init.body).text(), `token=${token}&agree=yes`);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
