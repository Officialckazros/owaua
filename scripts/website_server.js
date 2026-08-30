"use strict";

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const root = path.resolve(__dirname, "sites");
const port = Number.parseInt(process.env.PORT || "", 10);
const mimeTypes = new Map([
  [".css", "text/css; charset=utf-8"],
  [".gif", "image/gif"],
  [".html", "text/html; charset=utf-8"],
  [".ico", "image/x-icon"],
  [".jpeg", "image/jpeg"],
  [".jpg", "image/jpeg"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".png", "image/png"],
  [".svg", "image/svg+xml"],
  [".txt", "text/plain; charset=utf-8"],
  [".webp", "image/webp"],
  [".woff", "font/woff"],
  [".woff2", "font/woff2"],
]);

if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error("PORT must be a valid TCP port");
}

function requestedFile(url) {
  const pathname = decodeURIComponent(new URL(url, "http://localhost").pathname);
  const relative = pathname.replace(/^\/+/, "") || "index.html";
  const candidate = path.resolve(root, relative);
  if (candidate !== root && !candidate.startsWith(`${root}${path.sep}`)) {
    return null;
  }
  return candidate;
}

http
  .createServer((request, response) => {
    if (request.method !== "GET" && request.method !== "HEAD") {
      response.writeHead(405, { Allow: "GET, HEAD" });
      response.end();
      return;
    }
    let file;
    try {
      file = requestedFile(request.url || "/");
    } catch {
      response.writeHead(400);
      response.end();
      return;
    }
    if (!file) {
      response.writeHead(403);
      response.end();
      return;
    }
    fs.stat(file, (error, stats) => {
      if (error || !stats.isFile()) {
        response.writeHead(404);
        response.end();
        return;
      }
      response.writeHead(200, {
        "Content-Length": stats.size,
        "Content-Type": mimeTypes.get(path.extname(file).toLowerCase()) || "application/octet-stream",
        "X-Content-Type-Options": "nosniff",
      });
      if (request.method === "HEAD") {
        response.end();
        return;
      }
      fs.createReadStream(file).on("error", () => response.destroy()).pipe(response);
    });
  })
  .listen(port, "0.0.0.0", () => {
    console.log(`Static sites listening on ${port}`);
  });
