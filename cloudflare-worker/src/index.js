const DEFAULT_ORIGIN = "http://paid5.daki.cc:4204";
const ISRAEL_ORIGIN = "http://paid5.daki.cc:4272";
const ALLOWED_ORIGINS = new Set([DEFAULT_ORIGIN, ISRAEL_ORIGIN]);
const CANONICAL_HOST = "owaua.com";
const PUBLIC_HOSTS = new Set([
  "wearegays.net",
  "www.wearegays.net",
  "owaua.com",
  "www.owaua.com",
]);
const UPSTREAM_TIMEOUT_MS = 8_000;
const MAX_UPSTREAM_BYTES = 512 * 1024;
const MAX_DASHBOARD_REQUEST_BYTES = 1024 * 1024;
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

// SHA-256 of the canonical legal-page stylesheet in src/owaua/web.py.
const STYLE_HASH = "Gx/3ynQPDPzYdpsjdtnJStxTMXlaR0I6ltF7pNzCC9Y=";

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

function parseIsraelOrigin(rawOrigin) {
  const origin = new URL(rawOrigin || ISRAEL_ORIGIN);
  if (
    origin.protocol !== "http:" ||
    origin.username ||
    origin.password ||
    origin.port !== "4272" ||
    origin.pathname !== "/" ||
    origin.search ||
    origin.hash ||
    !ALLOWED_ORIGINS.has(origin.origin)
  ) {
    throw new TypeError("invalid configured Israel origin");
  }
  return origin;
}

function isIsraelPath(path) {
  return path === "/israel" || path.startsWith("/israel/");
}

function boundedAsn(value) {
  const asn = Number(value);
  return Number.isInteger(asn) && asn >= 1 && asn <= 4294967294 ? String(asn) : "";
}

function boundedOrg(value) {
  if (typeof value !== "string") {
    return "";
  }
  return value.replace(/[^\x20-\x7E]/g, "").trim().slice(0, 120);
}

function validClientIp(value) {
  if (typeof value !== "string" || value.length < 7 || value.length > 45) {
    return false;
  }
  if (/[^0-9A-Fa-f:.]/.test(value)) {
    return false;
  }
  if (value.includes(".")) {
    return /^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$/.test(
      value,
    );
  }
  const groups = value.split(":");
  return value.includes(":") && groups.length >= 3 && groups.length <= 8;
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
  if (path === "/owaua/terms/accept") {
    const clientIp = request.headers.get("CF-Connecting-IP") || "";
    if (validClientIp(clientIp)) {
      headers.set("X-Forwarded-For", clientIp);
    }
    const cf = request.cf && typeof request.cf === "object" ? request.cf : {};
    const asn = boundedAsn(cf.asn);
    const org = boundedOrg(cf.asOrganization);
    if (asn) {
      headers.set("X-Owaua-ASN", asn);
    }
    if (org) {
      headers.set("X-Owaua-AS-Org", org);
    }
    if (request.method === "POST") {
      if (typeof env.ORIGIN_AUTH_SECRET !== "string" || env.ORIGIN_AUTH_SECRET.length < 32) {
        throw new TypeError("missing acceptance proxy secret");
      }
      headers.set("Content-Type", "application/x-www-form-urlencoded");
      headers.set("X-Owaua-Origin-Auth", env.ORIGIN_AUTH_SECRET);
    } else if (
      typeof env.ORIGIN_AUTH_SECRET === "string" &&
      env.ORIGIN_AUTH_SECRET.length >= 32
    ) {
      headers.set("X-Owaua-Origin-Auth", env.ORIGIN_AUTH_SECRET);
    }
  }
  return headers;
}

function israelUpstreamHeaders(request, publicUrl, path, env) {
  const headers = new Headers({
    Accept: request.headers.get("Accept") || "text/html,application/json;q=0.9,*/*;q=0.8",
    "User-Agent": "owaua-Cloudflare-Proxy/2.0",
    "X-Forwarded-Host": publicUrl.host,
    "X-Forwarded-Proto": "https",
  });
  for (const name of ["Accept-Language", "Cookie", "X-CSRF-Token"]) {
    const value = request.headers.get(name);
    if (value && value.length <= 8192 && !/[\r\n]/.test(value)) {
      headers.set(name, value);
    }
  }
  const contentType = request.headers.get("Content-Type") || "";
  if (
    contentType.length <= 200 &&
    /^(?:application\/json|application\/x-www-form-urlencoded|multipart\/form-data)(?:;|$)/i.test(contentType)
  ) {
    headers.set("Content-Type", contentType);
  }
  const requestId = request.headers.get("CF-Ray");
  if (requestId && /^[A-Za-z0-9-]{1,64}$/.test(requestId)) {
    headers.set("X-Request-ID", requestId);
  }
  if (path === "/israel/terms/accept") {
    const clientIp = request.headers.get("CF-Connecting-IP") || "";
    if (validClientIp(clientIp)) {
      headers.set("X-Forwarded-For", clientIp);
    }
    const cf = request.cf && typeof request.cf === "object" ? request.cf : {};
    const asn = boundedAsn(cf.asn);
    const org = boundedOrg(cf.asOrganization);
    if (asn) headers.set("X-Owaua-ASN", asn);
    if (org) headers.set("X-Owaua-AS-Org", org);
    if (
      typeof env.ISRAEL_ORIGIN_AUTH_SECRET === "string" &&
      env.ISRAEL_ORIGIN_AUTH_SECRET.length >= 32
    ) {
      headers.set("X-Owaua-Origin-Auth", env.ISRAEL_ORIGIN_AUTH_SECRET);
    } else if (request.method === "POST") {
      throw new TypeError("missing Israel acceptance proxy secret");
    }
  }
  return headers;
}

function israelResponseHeaders(upstream, target, publicUrl) {
  const headers = new Headers(upstream.headers);
  for (const name of ["Connection", "Keep-Alive", "Proxy-Authenticate", "Proxy-Authorization", "Server", "Transfer-Encoding"]) {
    headers.delete(name);
  }
  const location = upstream.headers.get("Location");
  if (location && upstream.status >= 300 && upstream.status < 400) {
    try {
      const resolved = new URL(location, target);
      if (resolved.origin === target.origin && isIsraelPath(resolved.pathname)) {
        headers.set("Location", new URL(resolved.pathname + resolved.search, publicUrl.origin).toString());
      } else if (resolved.protocol === "https:" && resolved.hostname === "discord.com") {
        headers.set("Location", resolved.toString());
      } else {
        headers.delete("Location");
      }
    } catch {
      headers.delete("Location");
    }
  }
  headers.set("Strict-Transport-Security", "max-age=31536000; includeSubDomains");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("X-Owaua-Proxy", "cloudflare-israel");
  return headers;
}

async function proxyIsrael(request, env, publicUrl) {
  const path = publicUrl.pathname;
  const allowedMethods = new Set(["GET", "HEAD", "POST", "PUT"]);
  if (!allowedMethods.has(request.method)) {
    const rejected = response(path, "Method not allowed", 405);
    rejected.headers.set("Allow", "GET, HEAD, POST, PUT");
    return rejected;
  }
  const rawLength = request.headers.get("Content-Length");
  if (rawLength && (!/^\d{1,10}$/.test(rawLength) || Number(rawLength) > MAX_DASHBOARD_REQUEST_BYTES)) {
    return response(path, "Request too large", 413);
  }
  if (publicUrl.search.length > 4096) {
    return response(path, "Request URI too long", 414);
  }

  let target;
  try {
    const origin = parseIsraelOrigin(env.OWAIS_ORIGIN);
    const upstreamPath = path === "/israel/healthz"
      ? "/healthz"
      : path === "/israel/readyz"
        ? "/readyz"
        : path;
    target = new URL(upstreamPath + publicUrl.search, origin);
  } catch {
    return response(path, "Service unavailable", 502);
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers: israelUpstreamHeaders(request, publicUrl, path, env),
      body: request.method === "POST" || request.method === "PUT" ? request.body : undefined,
      redirect: "manual",
      signal: controller.signal,
    });
    const headers = israelResponseHeaders(upstream, target, publicUrl);
    if (request.method === "HEAD") {
      await upstream.body?.cancel("HEAD response body is not forwarded");
      return new Response(null, { status: upstream.status, headers });
    }
    return new Response(upstream.body, { status: upstream.status, headers });
  } catch {
    return response(path, "OWAIS is temporarily unavailable. Please try again shortly.", 502);
  } finally {
    clearTimeout(timeout);
  }
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
    const hostname = publicUrl.hostname.toLowerCase();
    if (
      !PUBLIC_HOSTS.has(hostname) ||
      (!PUBLIC_PATHS.has(path) && !isIsraelPath(path)) ||
      publicUrl.username ||
      publicUrl.password ||
      (publicUrl.port && publicUrl.port !== "443")
    ) {
      return response(path, "Not found", 404);
    }
    if (publicUrl.protocol !== "https:") {
      return response(path, "HTTPS is required", 400);
    }
    if (isIsraelPath(path)) {
      if (hostname !== CANONICAL_HOST) {
        publicUrl.hostname = CANONICAL_HOST;
        publicUrl.port = "";
        return Response.redirect(publicUrl, 308);
      }
      return await proxyIsrael(request, env, publicUrl);
    }
    const acceptingTerms = path === "/owaua/terms/accept" && request.method === "POST";
    if (request.method !== "GET" && request.method !== "HEAD" && !acceptingTerms) {
      const rejected = response(path, "Method not allowed", 405);
      rejected.headers.set("Allow", "GET, HEAD, POST");
      return rejected;
    }
    if (hostname !== CANONICAL_HOST) {
      publicUrl.hostname = CANONICAL_HOST;
      publicUrl.port = "";
      return Response.redirect(publicUrl, 308);
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
