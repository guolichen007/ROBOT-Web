// G3 正式浏览器验收：真实 Chromium 打开**生产构建的 Firebot Web**（登录 → 监控页），
// 通过监控页 VideoSurveillancePanel 内的生产 VideoCard「连接视频」建立 WHEP 会话（非独立测试页）。
// 输入（stdin JSON，凭据不进 argv）：{username, password}
// argv：<base_url> <vehicle_id> <out_dir> <phase>
//   phase=play      ：两路 VideoCard 连接 → 解码帧计数增长 + 像素哈希变化 → 截图；读温度 KPI
//   phase=temps     ：只读温度 KPI（配合外部模拟源逐值上报），按 stdin.expect 序列等待
//   phase=reconnect ：连接 → 等外部断流（stdin.signal_file 出现）→ 等恢复 → 再次解码
// 输出最后一行 JSON。
import { createRequire } from 'node:module'
import fs from 'node:fs'
import path from 'node:path'

const require = createRequire(import.meta.url)
const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../../../apps/web')
const { chromium } = require(path.join(root, 'node_modules/playwright-core'))
const [base, vehicleId, outDir, phase = 'play'] = process.argv.slice(2)
const input = JSON.parse(await new Promise((resolve) => {
  let d = ''
  process.stdin.setEncoding('utf8')
  process.stdin.on('data', (c) => (d += c))
  process.stdin.on('end', () => resolve(d || '{}'))
}))

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required'],
})
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } })
await context.addInitScript((vid) => localStorage.setItem('firebot.activeVehicleId', vid), vehicleId)
const page = await context.newPage()
const requests = []
page.on('request', (r) => requests.push(`${r.method()} ${new URL(r.url()).pathname}`))
const out = { phase, vehicle: vehicleId, cards: {}, temps: [], requests_media: 0 }

async function login() {
  await page.goto(`${base}/login`)
  await page.fill('#login-username', input.username)
  await page.fill('#login-password', input.password)
  await page.click('button.yd-login-btn')
  await page.waitForURL(/\/monitor/, { timeout: 30000 })
  await page.waitForSelector('.video-surveillance .hmi-camera', { timeout: 30000 })
}

function card(label) {
  return page.locator('.video-surveillance .hmi-camera', { has: page.locator('h3', { hasText: label }) })
}

// 在页面内采样某张生产 VideoCard 的 <video>：解码帧数 + 顶部/中部像素哈希
async function sample(label, seconds) {
  const handle = await card(label).locator('video').elementHandle()
  return page.evaluate(async ({ video, seconds }) => {
    const res = { frames: 0, width: 0, height: 0, hashes: [] }
    let frames = 0
    const tick = () => { frames += 1; video.requestVideoFrameCallback(tick) }
    video.requestVideoFrameCallback(tick)
    const canvas = document.createElement('canvas')
    const grab = () => {
      canvas.width = video.videoWidth; canvas.height = video.videoHeight
      const ctx = canvas.getContext('2d', { willReadFrequently: true })
      ctx.drawImage(video, 0, 0)
      const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data
      let h = 0
      for (let i = 0; i < data.length; i += 997) h = (h * 31 + data[i]) >>> 0
      const top = ctx.getImageData(Math.floor(canvas.width / 2), 4, 1, 1).data
      return { h, top: [top[0], top[1], top[2]] }
    }
    const start = performance.now()
    while (performance.now() - start < seconds * 1000) {
      await new Promise((r) => setTimeout(r, 1000))
      if (video.videoWidth) res.hashes.push(grab())
    }
    res.frames = frames
    res.width = video.videoWidth
    res.height = video.videoHeight
    res.distinct = new Set(res.hashes.map((x) => x.h)).size
    return res
  }, { video: handle, seconds })
}

async function connect(label) {
  const c = card(label)
  await c.getByRole('button', { name: '连接视频' }).click()
  await c.locator('.video-live-tag').waitFor({ timeout: 30000 })
}

async function readTemp() {
  const kpi = page.locator('[data-kpi="temperature"]')
  return { label: (await kpi.locator('span').textContent())?.trim(), value: (await kpi.locator('strong').textContent())?.trim() }
}

try {
  await login()
  out.banner = (await page.locator('[data-testid="simulated-source-banner"]').textContent().catch(() => null))?.trim() ?? null
  if (phase === 'play') {
    for (const [label, key] of [['监控相机', 'rgb'], ['红外热成像', 'thermal']]) {
      await connect(label)
      const s = await sample(label, 6)
      out.cards[key] = { ...s, live: true, decoded_changing: s.frames > 30 && s.width > 0 && s.distinct >= 3 }
      await card(label).screenshot({ path: path.join(outDir, `videocard-${key}.png`) })
    }
    out.temp = await readTemp()
    await page.screenshot({ path: path.join(outDir, 'monitor-page.png'), fullPage: false })
  } else if (phase === 'temps') {
    for (const expect of input.expect) {
      const deadline = Date.now() + 25000
      let t = await readTemp()
      while (t.value !== expect && Date.now() < deadline) { await page.waitForTimeout(500); t = await readTemp() }
      out.temps.push({ expect, ...t, ok: t.value === expect })
      if (input.ack_dir) fs.writeFileSync(path.join(input.ack_dir, `seen-${out.temps.length}`), t.value || '')
      await page.locator('.hmi-kpis').screenshot({ path: path.join(outDir, `temp-${out.temps.length}.png`) })
    }
  } else if (phase === 'reconnect') {
    const label = '监控相机'
    await connect(label)
    const before = await sample(label, 3)
    fs.writeFileSync(input.ready_file, 'connected')
    // 外部断流期间每 500ms 记录：LIVE 标签 / 占位文案 / 已解码帧数（页面内 rVFC 计数器）
    const vh = await card(label).locator('video').elementHandle()
    await page.evaluate((video) => {
      window.__frames = 0
      const tick = () => { window.__frames += 1; video.requestVideoFrameCallback(tick) }
      video.requestVideoFrameCallback(tick)
    }, vh)
    const timeline = []
    const t0 = Date.now()
    const deadline = t0 + 60000
    while (Date.now() < deadline && !fs.existsSync(input.restored_file)) {
      const live = (await card(label).locator('.video-live-tag').count()) > 0
      const ph = card(label).locator('.video-placeholder')
      const text = (await ph.count()) ? ((await ph.textContent({ timeout: 200 }).catch(() => '')) || '').trim() : ''
      const stale = (await card(label).locator('[data-video-stalled]').count()) > 0
      timeline.push({ t: Date.now() - t0, live, stale, text, frames: await page.evaluate(() => window.__frames) })
      await page.waitForTimeout(500)
    }
    out.timeline = timeline
    // 判定：断流期间存在“帧数停止增长”的区间，且该区间内 UI 不再宣称正常实时（离开 LIVE 或显示中断）
    // 帧停止增长后 3.5s（看门狗 3s + 采样间隔）起，UI 必须不再宣称「实时」
    let frozenSince = null
    const violations = []
    for (let i = 1; i < timeline.length; i += 1) {
      const x = timeline[i]
      if (x.frames === timeline[i - 1].frames) frozenSince ??= timeline[i - 1].t
      else frozenSince = null
      if (frozenSince !== null && x.t - frozenSince >= 3500 && x.live && !x.stale) violations.push(x)
    }
    out.frozen_samples = timeline.filter((x, i) => i > 0 && x.frames === timeline[i - 1].frames).length
    out.violations = violations.length
    out.first_not_live = timeline.find((x) => !x.live || x.stale) ?? null
    out.state_during_outage = out.frozen_samples > 0 && violations.length === 0 && out.first_not_live
      ? (out.first_not_live.stale ? 'stalled-indicator' : out.first_not_live.text || 'not-live')
      : false
    // 源恢复：生产 VideoCard 有界自动重连（1s/2s/4s）或用户再次点击「连接视频」
    let recovered = 'auto'
    try {
      await card(label).locator('.video-live-tag').waitFor({ timeout: 15000 })
    } catch {
      recovered = 'manual'
      await connect(label)
    }
    const after = await sample(label, 4)
    out.cards.rgb = { before, after, recovered, decoded_after: after.frames > 20 && after.distinct >= 2 }
    await card(label).screenshot({ path: path.join(outDir, 'videocard-rgb-reconnected.png') })
  }
  out.requests_media = requests.filter((r) => r.includes('/media/')).length
  out.requests = requests.filter((r) => /\/(api\/v1\/media|media\/)/.test(r))
  out.ok = true
} catch (err) {
  out.ok = false
  out.error = String(err).slice(0, 500)
  await page.screenshot({ path: path.join(outDir, `failure-${phase}.png`) }).catch(() => {})
} finally {
  await browser.close()
}
console.log(JSON.stringify(out))
