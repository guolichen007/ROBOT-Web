import http from 'node:http'
import { spawn } from 'node:child_process'

// 合成视频测试源（RGB/thermal 双流可多实例）。
// 输出协议：OUTPUT_PROTOCOL=rtsp（默认，?token=）或 srt（streamid 携带发布 token + 独立加密 passphrase）。
// V1 浏览器基准合同：H.264 + yuv420p（MediaMTX 不转码）。
const token = process.env.MEDIA_PUBLISH_TOKEN || 'test-media-publisher-token-2026'
const streamId = process.env.MEDIA_STREAM_ID || 'R001-roof_rgb'
const outputProtocol = (process.env.OUTPUT_PROTOCOL || 'rtsp').toLowerCase()
const rtspOrigin = process.env.MEDIA_RTSP_ORIGIN || 'rtsp://mediamtx:8554'
const srtOrigin = process.env.MEDIA_SRT_ORIGIN || 'srt://mediamtx:8890'
const srtPassphrase = process.env.SRT_PUBLISH_PASSPHRASE || ''

function buildTarget() {
  if (outputProtocol === 'srt') {
    // SRT 鉴权：streamid=publish:<path>:<user>:<pass>（MediaMTX authHTTP 校验 password==发布 token）。
    // SRT 加密：passphrase 与鉴权 token 严格独立（R2-16），不得复用。
    const streamid = `publish:${streamId}:u-dao:${encodeURIComponent(token)}`
    const url = new URL(srtOrigin)
    url.searchParams.set('streamid', streamid)
    if (srtPassphrase) url.searchParams.set('passphrase', srtPassphrase)
    url.searchParams.set('pkt_size', '1316')
    return url.toString()
  }
  return `${rtspOrigin}/${encodeURIComponent(streamId)}?token=${encodeURIComponent(token)}`
}

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
]
if (outputProtocol === 'srt') {
  args.push('-f', 'mpegts', buildTarget())
} else {
  args.push('-f', 'rtsp', '-rtsp_transport', 'tcp', buildTarget())
}

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
        JSON.stringify({
          service: 'media-test-source',
          streamId,
          protocol: outputProtocol,
          codec: 'video/H264',
          pixFmt: 'yuv420p',
          status: 'ready',
        }),
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
    response.end(
      JSON.stringify({ ready, codec: 'video/H264', pixFmt: 'yuv420p', streamId, protocol: outputProtocol, lastError }),
    )
  })
  .listen(8090, '0.0.0.0')

for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => ffmpeg.kill(signal))
}
