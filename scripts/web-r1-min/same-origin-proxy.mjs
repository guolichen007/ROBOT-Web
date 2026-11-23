// 同源反向代理：静态 dist + /api /ws /health → uvicorn（与生产 nginx 同源语义一致）
import http from "node:http";
import net from "node:net";
import fs from "node:fs";
import path from "node:path";
const DIST =
  process.env.DIST || new URL("../../apps/web/dist", import.meta.url).pathname;
const API = { host: "127.0.0.1", port: Number(process.env.API_PORT || 18000) };
const PORT = Number(process.env.PORT || 18080);
const types = {
  ".js": "text/javascript",
  ".css": "text/css",
  ".html": "text/html",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".woff2": "font/woff2",
};
const isApi = (u) =>
  u.startsWith("/api/") || u.startsWith("/ws/") || u.startsWith("/health");
const server = http.createServer((req, res) => {
  if (isApi(req.url)) {
    const p = http.request(
      { ...API, path: req.url, method: req.method, headers: req.headers },
      (r) => {
        res.writeHead(r.statusCode, r.headers);
        r.pipe(res);
      },
    );
    p.on("error", () => {
      res.writeHead(502);
      res.end("bad gateway");
    });
    req.pipe(p);
    return;
  }
  let file = path.join(DIST, decodeURIComponent(req.url.split("?")[0]));
  if (
    !file.startsWith(DIST) ||
    !fs.existsSync(file) ||
    fs.statSync(file).isDirectory()
  )
    file = path.join(DIST, "index.html");
  res.writeHead(200, {
    "content-type": types[path.extname(file)] || "application/octet-stream",
  });
  fs.createReadStream(file).pipe(res);
});
server.on("upgrade", (req, socket, head) => {
  const upstream = net.connect(API.port, API.host, () => {
    upstream.write(
      `${req.method} ${req.url} HTTP/1.1\r\n` +
        Object.entries(req.headers)
          .map(([k, v]) => `${k}: ${v}`)
          .join("\r\n") +
        "\r\n\r\n",
    );
    upstream.write(head);
    upstream.pipe(socket);
    socket.pipe(upstream);
  });
  upstream.on("error", () => socket.destroy());
  socket.on("error", () => upstream.destroy());
});
server.listen(PORT, "127.0.0.1", () => console.log("proxy on", PORT));
