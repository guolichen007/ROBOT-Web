import http from 'node:http'
import { spawn } from 'node:child_process'

const token = process.env.MEDIA_PUBLISH_TOKEN || 'test-media-publisher-token-2026'
const streamId = process.env.MEDIA_STREAM_ID || 'R001-roof_rgb'
const rtspOrigin = process.env.MEDIA_RTSP_ORIGIN || 'rtsp://mediamtx:8554'
const target = `${rtspOrigin}/${encodeURIComponent(streamId)}?token=${encodeURIComponent(token)}`
const args = [
  '-hide_banner',
  '-loglevel',
  'warning',
  '-re',
  '-f',
  'lavfi',
  '-i',
  'testsrc2=size=640x360:rate=15',
  '-an',
  '-c:v',
  'libx264',
  '-preset',
  'ultrafast',
  '-tune',
  'zerolatency',
  '-pix_fmt',
  'yuv420p',
  '-g',
  '30',
  '-f',
  'rtsp',
  '-rtsp_transport',
  'tcp',
  target,
]

let ready = false
let lastError = null
const ffmpeg = spawn('ffmpeg', args, { stdio: ['ignore', 'inherit', 'pipe'] })
ffmpeg.stderr.setEncoding('utf8')
ffmpeg.stderr.on('data', (data) => {
  lastError = String(data).trim().slice(-1000)
  process.stderr.write(data)
})
ffmpeg.on('spawn', () => {
  setTimeout(() => {
    if (ffmpeg.exitCode === null) {
      ready = true
      console.log(
        JSON.stringify({ service: 'media-test-source', streamId, codec: 'video/H264', status: 'ready' }),
      )
    }
  }, 2000)
})
ffmpeg.on('exit', (code) => {
  ready = false
  console.error(JSON.stringify({ service: 'media-test-source', status: 'failed', code, lastError }))
  setTimeout(() => process.exit(code || 1), 500)
})

http
  .createServer((_request, response) => {
    response.writeHead(ready ? 200 : 503, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ ready, codec: 'video/H264', streamId, lastError }))
  })
  .listen(8090, '0.0.0.0')

for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => ffmpeg.kill(signal))
}
