const MAX_BYTES = 32 * 1024;

function deny(status = 404) {
  return new Response("Not found", { status });
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return deny();
    }
    const expected = typeof env.LOG_TOKEN === "string" ? env.LOG_TOKEN : "";
    const auth = request.headers.get("Authorization") || "";
    if (!expected || expected.length < 32 || auth !== `Bearer ${expected}`) {
      return deny();
    }
    const rawLength = request.headers.get("Content-Length") || "";
    if (rawLength && (!/^\d{1,5}$/.test(rawLength) || Number(rawLength) > MAX_BYTES)) {
      return new Response("too large", { status: 413 });
    }
    const body = await request.text();
    if (body.length > MAX_BYTES) {
      return new Response("too large", { status: 413 });
    }
    try {
      const parsed = JSON.parse(body);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return new Response("invalid", { status: 400 });
      }
    } catch {
      return new Response("invalid", { status: 400 });
    }
    if (env.LOGS) {
      const day = new Date().toISOString().slice(0, 10);
      await env.LOGS.put(`${day}/${crypto.randomUUID()}.json`, body, {
        expirationTtl: 60 * 60 * 24 * 30,
      });
    }
    return new Response(null, { status: 204 });
  },
};
