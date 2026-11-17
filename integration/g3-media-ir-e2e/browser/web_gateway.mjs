// G3 正式浏览器验收用网关：零依赖复刻 infra/nginx/nginx.server.conf 的路由语义（仅隔离测试）。
//   /            → 生产构建的 Web 静态文件（SPA 回落 index.html）
//   /api/, /ws/  → API（含 WebSocket Upgrade）
//   /media/      → MediaMTX WHEP（去掉 /media 前缀；Location 回写 /media 前缀 = proxy_redirect / /media/）
// 用法：node web_gateway.mjs <port> <dist_dir> <api_port> <whep_port>
import http from 'node:http'
import net from 'node:net'
import fs from 'node:fs'
import path from 'node:path'

const [port, dist, apiPort, whepPort] = process.argv.slice(2)
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css',
  '.png': 'image/png', '.svg': 'image/svg+xml', '.json': 'application/json', '.woff2': 'font/woff2',
  '.ico': 'image/x-icon', '.webp': 'image/webp', '.jpg': 'image/jpeg' }

function proxy(req, res, targetPort, targetPath, rewriteLocation) {
  const upstream = http.request(
    { host: '127.0.0.1', port: Number(targetPort), method: req.method, path: targetPath,
      headers: { ...req.headers, host: `127.0.0.1:${targetPort}` } },
    (up) => {
      const headers = { ...up.headers }
      if (rewriteLocation && headers.location?.startsWith('/')) headers.location = '/media' + headers.location
      res.writeHead(up.statusCode || 502, headers)
      up.pipe(res)
    },
  )
  upstream.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end() })
  req.pipe(upstream)
}

const server = http.createServer((req, res) => {
  const url = req.url || '/'
  if (url.startsWith('/api/')) return proxy(req, res, apiPort, url, false)
  if (url.startsWith('/media/')) return proxy(req, res, whepPort, url.slice('/media'.length), true)
  const clean = decodeURIComponent(url.split('?')[0])
  let file = path.join(dist, clean)
  if (!file.startsWith(path.resolve(dist)) || !fs.existsSync(file) || fs.statSync(file).isDirectory())
    file = path.join(dist, 'index.html')
  res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' })
  fs.createReadStream(file).pipe(res)
})

server.on('upgrade', (req, socket, head) => {
  if (!(req.url || '').startsWith('/ws/')) return socket.destroy()
  const upstream = net.connect(Number(apiPort), '127.0.0.1', () => {
    const lines = [`${req.method} ${req.url} HTTP/1.1`]
    for (let i = 0; i < req.rawHeaders.length; i += 2) lines.push(`${req.rawHeaders[i]}: ${req.rawHeaders[i + 1]}`)
    upstream.write(lines.join('\r\n') + '\r\n\r\n')
    if (head?.length) upstream.write(head)
    socket.pipe(upstream).pipe(socket)
  })
  upstream.on('error', () => socket.destroy())
  socket.on('error', () => upstream.destroy())
})

server.listen(Number(port), '127.0.0.1', () => console.log(`GATEWAY_READY ${port}`))
