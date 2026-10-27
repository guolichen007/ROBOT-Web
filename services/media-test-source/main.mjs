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
    // 注意：streamid 的分号分隔结构必须保持原文；token/passphrase 作为“值”需编码
    // （base64 含 '+'，ffmpeg 会对查询值做 URL 解码，'+' 会被解码为空格导致鉴权/加密失败）。
    // per-vehicle SRT 发布语义：user 必须为流所属 canonical vehicle_id（默认 R001 合成流）
    const srtUser = process.env.SRT_USER || 'R001'
    const streamid = `publish:${streamId}:${srtUser}:${encodeURIComponent(token)}`
    let query = `streamid=${streamid}&pkt_size=1316`
    if (srtPassphrase) query += `&passphrase=${encodeURIComponent(srtPassphrase)}`
    return `${srtOrigin}?${query}`
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
