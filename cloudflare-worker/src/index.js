// This is the app's Daki allocation, not the Daki control-panel origin.
const DEFAULT_ORIGIN = "http://paid5.daki.cc:4204";
const ALLOWED_ORIGINS = new Set([DEFAULT_ORIGIN]);
const PUBLIC_HOSTS = new Set(["wearegays.net", "www.wearegays.net"]);
const UPSTREAM_TIMEOUT_MS = 8_000;
const MAX_UPSTREAM_BYTES = 512 * 1024;
const STATUS_COMPONENTS = new Set(["service", "discord", "database", "malware_scanner"]);
const LEGACY_SLUG = ["sef", "bot"].join("");
const LEGACY_PREFIX = `/${LEGACY_SLUG}`;
const LEGACY_PATHS = new Map([
  [LEGACY_PREFIX, "/owaua"],
  [`${LEGACY_PREFIX}/`, "/owaua"],
  [`${LEGACY_PREFIX}/terms`, "/owaua/terms"],
  [`${LEGACY_PREFIX}/terms/accept`, "/owaua/terms/accept"],
  [`${LEGACY_PREFIX}/privacy`, "/owaua/privacy"],
  [`${LEGACY_PREFIX}/tos`, "/owaua/terms"],
  [`/${["op", "sef"].join("")}-tos.html`, "/owaua/terms"],
  [`/${["op", "sef"].join("")}-privacy.html`, "/owaua/privacy"],
]);

const PUBLIC_PATHS = new Set([
  "/owaua",
  "/owaua/",
  "/owaua/terms",
  "/owaua/terms/accept",
  "/owaua/privacy",
  "/owaua/tos",
  "/owaua-tos.html",
  "/owaua-privacy.html",
  "/healthz",
  "/readyz",
  ...LEGACY_PATHS.keys(),
]);

const STYLE_HASH = "n/KWVDWzPnaDdVPU+wL3c/kqpG3S+iT7l3H/+a+XrUI=";

function securityHeaders(path, contentType, cacheable = false) {
  const health = path === "/healthz" || path === "/readyz";
  const formAction = path === "/owaua/terms/accept" ? "'self'" : "'none'";
  return {
    "Cache-Control": !health && cacheable ? "public, max-age=300" : "no-store",
    "Content-Security-Policy":
      `default-src 'none'; base-uri 'none'; form-action ${formAction}; ` +
      `style-src 'sha256-${STYLE_HASH}'; frame-ancestors 'none'`,
    "Content-Type": contentType,
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy":
      "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": !health && cacheable ? "index, follow" : "noindex, nofollow",
    "X-Owaua-Proxy": "cloudflare",
  };
}

function response(path, body, status, contentType = "text/plain; charset=utf-8") {
  return new Response(body, {
    status,
    headers: securityHeaders(path, contentType),
  });
}

function parseOrigin(rawOrigin) {
  const origin = new URL(rawOrigin || DEFAULT_ORIGIN);
  if (
    origin.protocol !== "http:" ||
    origin.username ||
    origin.password ||
    origin.port !== "4204" ||
    origin.pathname !== "/" ||
    origin.search ||
    origin.hash ||
    !ALLOWED_ORIGINS.has(origin.origin)
  ) {
    throw new TypeError("invalid configured origin");
  }
  return origin;
}

function upstreamHeaders(request, publicUrl, path, env) {
  const headers = new Headers({
    Accept:
      path === "/healthz" || path === "/readyz"
        ? "application/json"
        : "text/html",
    "User-Agent": "owaua-Cloudflare-Proxy/2.0",
    "X-Forwarded-Host": publicUrl.host,
    "X-Forwarded-Proto": "https",
  });
  const requestId = request.headers.get("CF-Ray");
  if (requestId && /^[A-Za-z0-9-]{1,64}$/.test(requestId)) {
    headers.set("X-Request-ID", requestId);
  }
  if (path === "/owaua/terms/accept" && request.method === "POST") {
    if (typeof env.ORIGIN_AUTH_SECRET !== "string" || env.ORIGIN_AUTH_SECRET.length < 32) {
      throw new TypeError("missing acceptance proxy secret");
    }
    headers.set("Content-Type", "application/x-www-form-urlencoded");
    headers.set("X-Owaua-Origin-Auth", env.ORIGIN_AUTH_SECRET);
  }
  return headers;
}

async function readBoundedBody(upstream) {
  const rawLength = upstream.headers.get("Content-Length");
  if (rawLength && !/^\d{1,10}$/.test(rawLength)) {
    await upstream.body?.cancel("invalid response length");
    throw new RangeError("invalid upstream length");
  }
  if (rawLength && Number(rawLength) > MAX_UPSTREAM_BYTES) {
    await upstream.body?.cancel("response size limit exceeded");
    throw new RangeError("upstream response is too large");
  }
  if (!upstream.body) {
    return null;
  }

  const reader = upstream.body.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    total += value.byteLength;
    if (total > MAX_UPSTREAM_BYTES) {
      await reader.cancel("response size limit exceeded");
      throw new RangeError("upstream response is too large");
    }
    chunks.push(value);
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

function sanitizedStatusBody(path, bytes, status) {
  let value;
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    value = JSON.parse(text);
  } catch {
    throw new TypeError("invalid status response");
  }
  if (!value || Array.isArray(value) || typeof value !== "object") {
    throw new TypeError("invalid status response");
  }
  if (path === "/healthz") {
    if (status !== 200 || value.status !== "ok") {
      throw new TypeError("invalid health response");
    }
    return JSON.stringify({ status: "ok" });
  }

  const expectedStatus = status === 200 ? "ready" : "not_ready";
  if (value.status !== expectedStatus || (status !== 200 && status !== 503)) {
    throw new TypeError("invalid readiness response");
  }
  const components = Object.entries(value).filter(([name]) => name !== "status");
  if (
    components.length === 0 ||
    components.length > STATUS_COMPONENTS.size ||
    components.some(
      ([name, ready]) =>
        !STATUS_COMPONENTS.has(name) || typeof ready !== "boolean",
    )
  ) {
    throw new TypeError("invalid readiness components");
  }
  return JSON.stringify(Object.fromEntries([["status", expectedStatus], ...components]));
}

async function proxiedResponse(path, upstream, target, publicUrl, method) {
  if (upstream.status >= 500 && !(path === "/readyz" && upstream.status === 503)) {
    await upstream.body?.cancel("upstream failure body is not forwarded");
    return response(
      path,
      "owaua is temporarily unavailable. Please try again shortly.",
      502,
    );
  }

  const expectedJson = path === "/healthz" || path === "/readyz";
  const upstreamType = (upstream.headers.get("Content-Type") || "")
    .split(";", 1)[0]
    .trim()
    .toLowerCase();
  const expectedType = expectedJson ? "application/json" : "text/html";
  if (
    (upstream.status < 300 || path === "/readyz") &&
    upstreamType !== expectedType
  ) {
    await upstream.body?.cancel("unexpected response type");
    return response(path, "Invalid response from service", 502);
  }
  const contentType = expectedJson
    ? "application/json; charset=utf-8"
    : "text/html; charset=utf-8";
  const headers = new Headers(
    securityHeaders(
      path,
      contentType,
      !expectedJson && upstream.status === 200 && PUBLIC_PATHS.has(path) && path !== "/owaua/terms/accept",
    ),
  );

  if (!expectedJson) {
    const etag = upstream.headers.get("ETag");
    if (etag && etag.length <= 200) {
      headers.set("ETag", etag);
    }
    const lastModified = upstream.headers.get("Last-Modified");
    if (lastModified && lastModified.length <= 100) {
      headers.set("Last-Modified", lastModified);
    }
  }

  const location = upstream.headers.get("Location");
  if (location && upstream.status >= 300 && upstream.status < 400) {
    try {
      const resolved = new URL(location, target);
      if (resolved.origin === target.origin && PUBLIC_PATHS.has(resolved.pathname)) {
        headers.set(
          "Location",
          new URL(resolved.pathname, publicUrl.origin).toString(),
        );
      }
    } catch {
      // A malformed redirect is deliberately omitted.
    }
  }

  let body = null;
  if (
    method !== "HEAD" &&
    (upstream.status < 300 || (path === "/readyz" && upstream.status === 503))
  ) {
    const boundedBody = await readBoundedBody(upstream);
    try {
      body = expectedJson
        ? sanitizedStatusBody(path, boundedBody || new Uint8Array(), upstream.status)
        : boundedBody;
    } catch {
      return response(path, "Invalid response from service", 502);
    }
  } else if (upstream.body) {
    await upstream.body.cancel("response body is not forwarded");
  }

  return new Response(body, {
    status: upstream.status,
    headers,
  });
}

export default {
  async fetch(request, env = {}) {
    const publicUrl = new URL(request.url);
    const path = publicUrl.pathname;
    if (
      !PUBLIC_HOSTS.has(publicUrl.hostname.toLowerCase()) ||
      !PUBLIC_PATHS.has(path) ||
      publicUrl.username ||
      publicUrl.password ||
      (publicUrl.port && publicUrl.port !== "443")
    ) {
      return response(path, "Not found", 404);
    }
    if (publicUrl.protocol !== "https:") {
      return response(path, "HTTPS is required", 400);
    }
    const canonicalPath = LEGACY_PATHS.get(path);
    if (canonicalPath) {
      if (request.method !== "GET" && request.method !== "HEAD") {
        const rejected = response(path, "Method not allowed", 405);
        rejected.headers.set("Allow", "GET, HEAD");
        return rejected;
      }
      publicUrl.pathname = canonicalPath;
      return Response.redirect(publicUrl, 308);
    }
    const acceptingTerms = path === "/owaua/terms/accept" && request.method === "POST";
    if (request.method !== "GET" && request.method !== "HEAD" && !acceptingTerms) {
      const rejected = response(path, "Method not allowed", 405);
      rejected.headers.set("Allow", "GET, HEAD, POST");
      return rejected;
    }

    let target;
    try {
      const origin = parseOrigin(env.OWAUA_ORIGIN);
      target = new URL(path, origin);
      if (path === "/owaua/terms/accept" && request.method === "GET") {
        const token = publicUrl.searchParams.get("token") || "";
        if (!/^[A-Za-z0-9_-]{40,80}$/.test(token) || [...publicUrl.searchParams.keys()].some((key) => key !== "token")) {
          return response(path, "Invalid acceptance link", 400);
        }
        target.searchParams.set("token", token);
      }
    } catch {
      return response(path, "Service unavailable", 502);
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
    try {
      const upstream = await fetch(target, {
        method: request.method,
        headers: upstreamHeaders(request, publicUrl, path, env),
        body: acceptingTerms ? request.body : undefined,
        redirect: "manual",
        signal: controller.signal,
      });
      return await proxiedResponse(path, upstream, target, publicUrl, request.method);
    } catch {
      return response(
        path,
        "owaua is temporarily unavailable. Please try again shortly.",
        502,
      );
    } finally {
      clearTimeout(timeout);
    }
  },
};
