// Loopback-only stand-in for infra/nginx (test stack): forwards to `vite preview` and adds the
// same X-Firebot-E2E-Target: test header the test nginx adds, so the repo's own e2e target
// guard can identify this sandbox stack. Refuses to bind or forward to anything but 127.0.0.1.
// usage: node scripts/v21-e2e-proxy.mjs <listenPort> <upstreamPort>
/* global process, console */
import http from 'node:http'
import net from 'node:net'

const [listen, upstream] = process.argv.slice(2).map(Number)
if (!listen || !upstream) {
  console.error('usage: v21-e2e-proxy.mjs <listenPort> <upstreamPort>')
  process.exit(2)
}
const HOST = '127.0.0.1'
const server = http.createServer((req, res) => {
  const proxied = http.request(
    { host: HOST, port: upstream, method: req.method, path: req.url, headers: req.headers },
    (up) => {
      res.writeHead(up.statusCode || 502, { ...up.headers, 'x-firebot-e2e-target': 'test' })
      up.pipe(res)
    },
  )
  proxied.on('error', () => {
    if (!res.headersSent) res.writeHead(502)
    res.end()
  })
  // a client that aborts (e.g. a test closing a page) must never take the proxy down
  req.on('error', () => proxied.destroy())
  res.on('error', () => proxied.destroy())
  req.pipe(proxied)
})
server.on('upgrade', (req, socket, head) => {
  socket.on('error', () => socket.destroy())
  const up = net.connect(upstream, HOST, () => {
    up.write(`${req.method} ${req.url} HTTP/1.1\r\n`)
    for (const [k, v] of Object.entries(req.headers)) up.write(`${k}: ${v}\r\n`)
    up.write('\r\n')
    up.write(head)
    up.pipe(socket)
    socket.pipe(up)
  })
  up.on('error', () => socket.destroy())
  socket.on('close', () => up.destroy())
})
server.on('clientError', (_err, socket) => socket.destroy())
process.on('uncaughtException', (err) => console.error('proxy kept alive after', err.code || err))
server.listen(listen, HOST, () => console.log(`e2e proxy ${HOST}:${listen} -> ${HOST}:${upstream}`))
