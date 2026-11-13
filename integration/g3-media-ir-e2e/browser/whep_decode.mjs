// G3 浏览器级媒体验收：真实 Chromium WebRTC（无 mock）→ MediaMTX WHEP（Authorization: Bearer 媒体票据）。
// 严格分三级输出：
//   whep_status        —— WHEP POST 返回码（201 = 仅协商成功）
//   ice_connected      —— RTCPeerConnection.connectionState/iceConnectionState 达到 connected（媒体连接建立）
//   decoded_changing   —— <video> 真实解码出帧（requestVideoFrameCallback 计数、videoWidth>0），且相隔
//                         一段时间抓取的两帧像素哈希不同（画面在变化，不是静帧）
// 用法：node whep_decode.mjs <whep_url> <ticket> <seconds>
// 依赖：apps/web/node_modules/playwright-core（CHROMIUM_PATH 指向 chrome-headless-shell）
import { createRequire } from 'node:module'
import path from 'node:path'

const require = createRequire(import.meta.url)
const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../../../apps/web')
const { chromium } = require(path.join(root, 'node_modules/playwright-core'))

const [whepUrl, ticket, seconds = '6'] = process.argv.slice(2)
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required'],
})
const page = await browser.newPage()
const result = await page.evaluate(
  async ({ whepUrl, ticket, seconds }) => {
    const out = { whep_status: null, ice_connected: false, frames: 0, width: 0, height: 0, hashes: [] }
    const pc = new RTCPeerConnection()
    pc.addTransceiver('video', { direction: 'recvonly' })
    const video = document.createElement('video')
    video.muted = true
    video.playsInline = true
    document.body.appendChild(video)
    pc.ontrack = (event) => {
      video.srcObject = event.streams[0]
      video.play().catch(() => {})
    }
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'connected') out.ice_connected = true
    }
    pc.oniceconnectionstatechange = () => {
      if (['connected', 'completed'].includes(pc.iceConnectionState)) out.ice_connected = true
    }
    const offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    await new Promise((resolve) => {
      if (pc.iceGatheringState === 'complete') return resolve()
      pc.onicegatheringstatechange = () => pc.iceGatheringState === 'complete' && resolve()
      setTimeout(resolve, 3000)
    })
    const response = await fetch(whepUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/sdp', Authorization: `Bearer ${ticket}` },
      body: pc.localDescription.sdp,
    })
    out.whep_status = response.status
    if (response.status !== 201) {
      out.body = (await response.text()).slice(0, 200)
      return out
    }
    await pc.setRemoteDescription({ type: 'answer', sdp: await response.text() })
    const countFrames = () =>
      video.requestVideoFrameCallback(() => {
        out.frames += 1
        countFrames()
      })
    countFrames()
    const canvas = document.createElement('canvas')
    const hashFrame = () => {
      if (!video.videoWidth) return null
      canvas.width = video.videoWidth
      canvas.height = video.videoHeight
      const ctx = canvas.getContext('2d')
      ctx.drawImage(video, 0, 0)
      const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data
      let h = 2166136261
      for (let i = 0; i < data.length; i += 97) h = Math.imul(h ^ data[i], 16777619)
      return { hash: (h >>> 0).toString(16), top: Array.from(ctx.getImageData(5, 5, 1, 1).data.slice(0, 3)) }
    }
    const deadline = Date.now() + seconds * 1000
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1000))
      const frame = hashFrame()
      if (frame) out.hashes.push(frame)
    }
    out.width = video.videoWidth
    out.height = video.videoHeight
    video.style.width = '640px'
    // 保持连接以便截图（截图后由 browser.close 结束会话）
    window.__pc = pc
    return out
  },
  { whepUrl, ticket, seconds: Number(seconds) },
)
if (process.env.SCREENSHOT_PATH && result.decoded_changing !== false) {
  await page.screenshot({ path: process.env.SCREENSHOT_PATH })
}
await browser.close()
const distinct = new Set(result.hashes.map((h) => h.hash)).size
result.decoded_changing = result.frames > 5 && result.width > 0 && distinct >= 2
result.distinct_frames = distinct
console.log(JSON.stringify(result))
