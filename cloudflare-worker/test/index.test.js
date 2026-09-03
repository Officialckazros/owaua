import assert from "node:assert/strict";
import test from "node:test";

import worker from "../src/index.js";


const ORIGIN = "http://paid5.daki.cc:4204";
const ISRAEL_ORIGIN = "http://paid5.daki.cc:4272";

test("rejects unknown paths and methods without contacting upstream", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    throw new Error("should not be called");
  };
  try {
    const missing = await worker.fetch(new Request("https://owaua.com/private"), {});
    assert.equal(missing.status, 404);
    assert.equal(missing.headers.get("X-Frame-Options"), "DENY");
    assert.equal(missing.headers.get("Cache-Control"), "no-store");

    const wrongHost = await worker.fetch(
      new Request("https://untrusted.example/owaua"),
      {},
    );
    assert.equal(wrongHost.status, 404);

    const insecure = await worker.fetch(
      new Request("http://owaua.com/owaua"),
      {},
    );
    assert.equal(insecure.status, 400);

    const post = await worker.fetch(
      new Request("https://owaua.com/owaua", { method: "POST", body: "data" }),
      {},
    );
    assert.equal(post.status, 405);
    assert.equal(post.headers.get("Allow"), "GET, HEAD, POST");
    assert.equal(calls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("uses the current legal stylesheet hash", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("<html>ok</html>", {
    status: 200,
    headers: { "Content-Type": "text/html" },
  });
  try {
    const result = await worker.fetch(new Request("https://owaua.com/owaua"), {
      OWAUA_ORIGIN: ORIGIN,
    });
    assert.match(
      result.headers.get("Content-Security-Policy"),
      /style-src 'sha256-Gx\/3ynQPDPzYdpsjdtnJStxTMXlaR0I6ltF7pNzCC9Y='/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("permanently redirects pre-rename legal links to owaua", async () => {
  const oldSlug = ["sef", "bot"].join("");
  const token = "a".repeat(40);
  const result = await worker.fetch(
    new Request(`https://owaua.com/${oldSlug}/terms/accept?token=${token}`),
  );
  assert.equal(result.status, 308);
  assert.equal(
    result.headers.get("Location"),
    `https://owaua.com/owaua/terms/accept?token=${token}`,
  );
});

test("permanently redirects old and www hosts to owaua.com", async () => {
  for (const host of ["wearegays.net", "www.wearegays.net", "www.owaua.com"]) {
    const result = await worker.fetch(
      new Request(`https://${host}/owaua/terms?source=bookmark`),
    );
    assert.equal(result.status, 308);
    assert.equal(
      result.headers.get("Location"),
      "https://owaua.com/owaua/terms?source=bookmark",
    );
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
    const request = new Request("https://owaua.com/owaua/terms?token=secret", {
      headers: {
        Authorization: "Bearer caller-secret",
        Cookie: "session=secret",
        "CF-Ray": "abc-123",
        "X-Forwarded-For": "127.0.0.1",
      },
    });
    const result = await worker.fetch(request, { OWAUA_ORIGIN: ORIGIN });
    assert.equal(result.status, 200);
    assert.equal(captured.url, `${ORIGIN}/owaua/terms`);
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
        Location: external ? "https://malicious.example/" : "/owaua/terms",
      },
    });
  try {
    const request = new Request("https://owaua.com/owaua/tos");
    const local = await worker.fetch(request, { OWAUA_ORIGIN: ORIGIN });
    assert.equal(local.headers.get("Location"), "https://owaua.com/owaua/terms");

    external = true;
    const blocked = await worker.fetch(request, { OWAUA_ORIGIN: ORIGIN });
    assert.equal(blocked.headers.get("Location"), null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("rejects unsafe origins and sanitizes upstream failures", async () => {
  const badOrigin = await worker.fetch(new Request("https://owaua.com/owaua"), {
    OWAUA_ORIGIN: "http://127.0.0.1:8080/path?secret=value",
  });
  assert.equal(badOrigin.status, 502);
  assert.equal(await badOrigin.text(), "Service unavailable");

  const untrustedOrigin = await worker.fetch(
      new Request("https://owaua.com/owaua"),
    { OWAUA_ORIGIN: "https://attacker.example" },
  );
  assert.equal(untrustedOrigin.status, 502);

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response("database password: secret", { status: 500 });
  try {
    const result = await worker.fetch(
      new Request("https://owaua.com/readyz"),
      { OWAUA_ORIGIN: ORIGIN },
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
      new Request("https://owaua.com/owaua", { method: "HEAD" }),
      { OWAUA_ORIGIN: ORIGIN },
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
    const request = new Request("https://owaua.com/readyz");
    const pending = await worker.fetch(request, { OWAUA_ORIGIN: ORIGIN });
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
    const leaked = await worker.fetch(request, { OWAUA_ORIGIN: ORIGIN });
    assert.equal(leaked.status, 502);
    assert.doesNotMatch(await leaked.text(), /secret|password/);

    globalThis.fetch = async () =>
      new Response("<script>unexpected</script>", {
        headers: { "Content-Type": "text/html" },
      });
    const confused = await worker.fetch(request, { OWAUA_ORIGIN: ORIGIN });
    assert.equal(confused.status, 502);
    assert.doesNotMatch(await confused.text(), /script|unexpected/);

    globalThis.fetch = async () =>
      new Response('{"status":"ready"}', {
        headers: {
          "Content-Type": "application/json",
          "Content-Length": "999999999",
        },
      });
    const oversized = await worker.fetch(request, { OWAUA_ORIGIN: ORIGIN });
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
    const getRequest = new Request(
      `https://owaua.com/owaua/terms/accept?token=${token}`,
      { headers: { "CF-Connecting-IP": "203.0.113.9" } },
    );
    Object.defineProperty(getRequest, "cf", {
      value: { asn: 13335, asOrganization: "Cloudflare, Inc." },
    });
    const get = await worker.fetch(getRequest, {
      OWAUA_ORIGIN: ORIGIN,
      ORIGIN_AUTH_SECRET: "s".repeat(32),
    });
    assert.equal(get.status, 200);
    assert.equal(captured[0].url, `${ORIGIN}/owaua/terms/accept?token=${token}`);
    assert.equal(captured[0].init.headers.get("X-Forwarded-For"), "203.0.113.9");
    assert.equal(captured[0].init.headers.get("X-Owaua-Origin-Auth"), "s".repeat(32));
    assert.equal(captured[0].init.headers.get("X-Owaua-ASN"), "13335");
    assert.equal(captured[0].init.headers.get("X-Owaua-AS-Org"), "Cloudflare, Inc.");

    const post = await worker.fetch(
      new Request("https://owaua.com/owaua/terms/accept", {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "CF-Connecting-IP": "203.0.113.9",
        },
        body: `token=${token}&agree=yes`,
      }),
      { OWAUA_ORIGIN: ORIGIN, ORIGIN_AUTH_SECRET: "s".repeat(32) },
    );
    assert.equal(post.status, 200);
    assert.equal(captured[1].init.headers.get("X-Owaua-Origin-Auth"), "s".repeat(32));
    assert.equal(captured[1].init.headers.get("X-Forwarded-For"), "203.0.113.9");
    assert.equal(await new Response(captured[1].init.body).text(), `token=${token}&agree=yes`);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("routes only the isolated Israel surface to the secondary Daki allocation", async () => {
  const originalFetch = globalThis.fetch;
  const captured = [];
  globalThis.fetch = async (url, init) => {
    captured.push({ url: url.toString(), init });
    const pathname = new URL(url).pathname;
    if (pathname.endsWith("app.css")) {
      return new Response("body{}", { headers: { "Content-Type": "text/css" } });
    }
    return new Response('<html lang="he-IL" dir="rtl">OWAIS</html>', {
      headers: { "Content-Type": "text/html", "Set-Cookie": "owais=session; Secure; HttpOnly; Path=/israel/dashboard" },
    });
  };
  try {
    const env = { OWAIS_ORIGIN: ISRAEL_ORIGIN };
    const page = await worker.fetch(new Request("https://owaua.com/israel/dashboard"), env);
    assert.equal(page.status, 200);
    assert.equal(captured[0].url, `${ISRAEL_ORIGIN}/israel/dashboard`);
    assert.equal(page.headers.get("X-Owaua-Proxy"), "cloudflare-israel");
    assert.match(page.headers.get("Set-Cookie"), /Path=\/israel\/dashboard/);

    const asset = await worker.fetch(new Request("https://owaua.com/israel/dashboard/assets/app.css"), env);
    assert.equal(asset.headers.get("Content-Type"), "text/css");
    assert.equal(captured[1].url, `${ISRAEL_ORIGIN}/israel/dashboard/assets/app.css`);

    const original = await worker.fetch(new Request("https://owaua.com/owaua"), {
      OWAUA_ORIGIN: ORIGIN,
    });
    assert.equal(captured[2].url, `${ORIGIN}/owaua`);
    assert.equal(original.headers.get("X-Owaua-Proxy"), "cloudflare");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("preserves Israel dashboard OAuth and API state without leaking caller headers", async () => {
  const originalFetch = globalThis.fetch;
  const captured = [];
  globalThis.fetch = async (url, init) => {
    captured.push({ url: url.toString(), init });
    if (captured.length === 1) {
      return new Response(null, {
        status: 303,
        headers: { Location: "https://discord.com/oauth2/authorize?client_id=1545108274607562812" },
      });
    }
    return Response.json({ ok: true });
  };
  try {
    const env = { OWAIS_ORIGIN: ISRAEL_ORIGIN };
    const oauth = await worker.fetch(
      new Request("https://owaua.com/israel/dashboard/auth/discord", {
        headers: { Authorization: "Bearer must-not-forward", Cookie: "owais=allowed" },
      }),
      env,
    );
    assert.equal(oauth.status, 303);
    assert.match(oauth.headers.get("Location"), /^https:\/\/discord\.com\/oauth2\/authorize/);
    assert.equal(captured[0].init.headers.get("Authorization"), null);
    assert.equal(captured[0].init.headers.get("Cookie"), "owais=allowed");

    const api = await worker.fetch(
      new Request("https://owaua.com/israel/dashboard/api/guild/123/settings", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": "csrf-token",
          Cookie: "owais=allowed",
        },
        body: "{}",
      }),
      env,
    );
    assert.equal(api.status, 200);
    assert.equal(captured[1].url, `${ISRAEL_ORIGIN}/israel/dashboard/api/guild/123/settings`);
    assert.equal(captured[1].init.headers.get("X-CSRF-Token"), "csrf-token");
    assert.equal(await new Response(captured[1].init.body).text(), "{}");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("maps prefixed Israel health checks and rejects unsafe secondary origins", async () => {
  const originalFetch = globalThis.fetch;
  let capturedUrl = "";
  globalThis.fetch = async (url) => {
    capturedUrl = url.toString();
    return Response.json({ status: "ready", discord: true });
  };
  try {
    const healthy = await worker.fetch(new Request("https://owaua.com/israel/readyz"), {
      OWAIS_ORIGIN: ISRAEL_ORIGIN,
    });
    assert.equal(healthy.status, 200);
    assert.equal(capturedUrl, `${ISRAEL_ORIGIN}/readyz`);

    const unsafe = await worker.fetch(new Request("https://owaua.com/israel"), {
      OWAIS_ORIGIN: "http://paid5.daki.cc:4204",
    });
    assert.equal(unsafe.status, 502);
    assert.equal(await unsafe.text(), "Service unavailable");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
