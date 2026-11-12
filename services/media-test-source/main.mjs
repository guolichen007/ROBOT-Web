import http from 'node:http'
import { spawn } from 'node:child_process'
import { redact } from './lib/redact.mjs'

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
    // SRT_USER 显式为空字符串 = 合成流（全局合成 token，user 为空）
    const srtUser = process.env.SRT_USER ?? 'R001'
    const streamid = `publish:${streamId}:${srtUser}:${encodeURIComponent(token)}`
    let query = `streamid=${streamid}&pkt_size=1316`
    if (srtPassphrase) query += `&passphrase=${encodeURIComponent(srtPassphrase)}`
    return `${srtOrigin}?${query}`
  }
  // RTSP 发布身份（G3）：真实车辆流必须以车辆身份（user=vehicle_id、password=该车派生 token）
  // 发布；只有登记为 SYNTHETIC_TEST 的合成流才使用全局合成 token（?token=）。
  const rtspUser = process.env.RTSP_USER || ''
  if (rtspUser) {
    const origin = new URL(rtspOrigin)
    origin.username = encodeURIComponent(rtspUser)
    origin.password = encodeURIComponent(token)
    return `${origin.toString().replace(/\/$/, '')}/${encodeURIComponent(streamId)}`
  }
  return `${rtspOrigin}/${encodeURIComponent(streamId)}?token=${encodeURIComponent(token)}`
}

// G3：模拟视频必须带明显 SIMULATED 标志（SIMULATED_LABEL 非空时叠加；thermal 采用伪彩）。
function overlayArgs() {
  const label = process.env.SIMULATED_LABEL || ''
  if (!label) return []
  const safe = label.replace(/[^A-Za-z0-9 _.-]/g, '')
  const filters = []
  if ((process.env.SIMULATED_KIND || '').toLowerCase() === 'thermal') {
    // 伪彩需作用于 YUV 帧（gray 帧上 pseudocolor 不生效，实测）
    filters.push('format=gray', 'format=yuv420p', 'pseudocolor=p=inferno')
  }
  filters.push(
    'drawbox=x=0:y=0:w=iw:h=48:color=red@0.85:t=fill',
    `drawtext=text='SIMULATED ${safe}':x=12:y=12:fontsize=22:fontcolor=white`,
    "drawtext=text='%{localtime}':x=12:y=h-36:fontsize=20:fontcolor=yellow:box=1:boxcolor=black@0.6",
  )
  return ['-vf', filters.join(',')]
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
  ...overlayArgs(),
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
// P0-B：ffmpeg/libSRT stderr 可能包含完整 SRT URL（streamid 内发布 token + passphrase
// 查询值）。按行缓冲 + redact 后才写 console/持久日志，避免跨 chunk 切分造成漏脱敏。
let stderrPending = ''
ffmpeg.stderr.on('data', (data) => {
  stderrPending += String(data)
  let idx
  while ((idx = stderrPending.indexOf('\n')) !== -1) {
    const line = redact(stderrPending.slice(0, idx + 1))
    stderrPending = stderrPending.slice(idx + 1)
    lastError = (lastError + line).slice(-1000)
    process.stderr.write(line)
  }
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
  // 退出前冲刷行缓冲（同样经 redact），确保任何残留 stderr 都不带 credential 落盘
  if (stderrPending) {
    lastError = (lastError + redact(stderrPending)).slice(-1000)
    stderrPending = ''
  }
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
  .listen(Number(process.env.HEALTH_PORT || 8090), process.env.HEALTH_HOST || '0.0.0.0')

for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => ffmpeg.kill(signal))
}
