import http from 'node:http'
import { spawn } from 'node:child_process'
import { createHmac } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { redact } from './lib/redact.mjs'

// 合成视频测试源（RGB/thermal 双流可多实例）。
// 输出协议：OUTPUT_PROTOCOL=rtsp（默认）或 srt（streamid 携带派生发布口令 + 独立加密 passphrase）。
// V1 浏览器基准合同：H.264 + yuv420p（MediaMTX 不转码）。
const streamId = process.env.MEDIA_STREAM_ID || 'R001-roof_rgb'
const outputProtocol = (process.env.OUTPUT_PROTOCOL || 'rtsp').toLowerCase()

// G3 凭证交付边界：
// - 主密钥（MEDIA_PUBLISH_MASTER_FILE / MEDIA_PUBLISH_MASTER）只在本进程内用于派生，
//   绝不出现在 ffmpeg argv、日志或健康检查输出中；
// - 交给 ffmpeg 的只有“按身份/流派生”的一次性发布口令：车辆流 = HMAC(master,"vehicle-srt:<id>")，
//   合成流 = HMAC(master,"synthetic-media:<stream>")，泄露后只影响该车/该合成流、可随主密钥轮换；
// - 兼容：MEDIA_PUBLISH_TOKEN 直接给出已派生口令（生产 compose.media-e2e.yml 现有用法）。
// ffmpeg 子进程 argv 对同用户/同容器进程可见（/proc/<pid>/cmdline）：合成源只在专用、
// 测完即停的隔离容器中以非 root 用户运行；stderr 经 redact 后才落日志。
function readMaster() {
  const file = process.env.MEDIA_PUBLISH_MASTER_FILE
  if (file) return readFileSync(file, 'utf8').trim()
  return process.env.MEDIA_PUBLISH_MASTER || ''
}

function derive(master, label) {
  return createHmac('sha256', master).update(label).digest('hex')
}

function publishToken() {
  if (process.env.MEDIA_PUBLISH_TOKEN) return process.env.MEDIA_PUBLISH_TOKEN
  const master = readMaster()
  if (!master) throw new Error('MEDIA_PUBLISH_TOKEN or MEDIA_PUBLISH_MASTER(_FILE) required')
  const vehicle = outputProtocol === 'srt' ? process.env.SRT_USER ?? 'R001' : process.env.RTSP_USER || ''
  return vehicle ? derive(master, `vehicle-srt:${vehicle}`) : derive(master, `synthetic-media:${streamId}`)
}

const token = publishToken()
// 主密钥相关环境变量不传给子进程
const childEnv = { ...process.env }
delete childEnv.MEDIA_PUBLISH_MASTER
delete childEnv.MEDIA_PUBLISH_MASTER_FILE
delete childEnv.MEDIA_PUBLISH_TOKEN
delete childEnv.SRT_PUBLISH_PASSPHRASE
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
const ffmpeg = spawn('ffmpeg', args, { stdio: ['ignore', 'inherit', 'pipe'], env: childEnv })
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
