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


async function probe(label) {
  const c = card(label)
  const live = (await c.locator('.video-live-tag').count()) > 0
  const stale = (await c.locator('[data-video-stalled]').count()) > 0
  const ff = await c.locator('video').getAttribute('data-first-frame').catch(() => null)
  const ph = c.locator('.video-placeholder')
  const text = (await ph.count()) ? ((await ph.textContent({ timeout: 200 }).catch(() => '')) || '').trim() : ''
  return { live, stale, first_frame: ff, text }
}

// 点击「连接视频」后 100ms 采样：记录首帧门控（live 标签只能在 data-first-frame=yes 后出现）
async function connectGated(label) {
  clicks += 1
  const c = card(label)
  const t0 = Date.now()
  await c.getByRole('button', { name: '连接视频' }).click()
  const samples = []
  while (Date.now() - t0 < 15000) {
    const p = await probe(label)
    samples.push({ t: Date.now() - t0, ...p })
    if (p.live) break
    await page.waitForTimeout(100)
  }
  const liveAt = samples.find((x) => x.live)?.t ?? null
  const violation = samples.some((x) => x.live && x.first_frame !== 'yes')
  const connectingSeen = samples.some((x) => !x.live && /连接|首帧/.test(x.text))
  return { live_at_ms: liveAt, violation, connecting_seen: connectingSeen, samples: samples.length }
}

// 两路同时：每秒记录各自解码帧数（页面内 rVFC 计数）、像素哈希、停滞/实时标签
async function dualSample(labels, seconds) {
  const handles = []
  for (const l of labels) handles.push(await card(l).locator('video').elementHandle())
  await page.evaluate((videos) => {
    window.__dual = videos.map(() => 0)
    videos.forEach((v, i) => {
      const tick = () => { window.__dual[i] += 1; v.requestVideoFrameCallback(tick) }
      v.requestVideoFrameCallback(tick)
    })
  }, handles)
  const rows = []
  for (let sec = 1; sec <= seconds; sec += 1) {
    await page.waitForTimeout(1000)
    const counts = await page.evaluate(() => [...window.__dual])
    const hashes = await page.evaluate((videos) => videos.map((video) => {
      const canvas = document.createElement('canvas')
      canvas.width = video.videoWidth; canvas.height = video.videoHeight
      if (!canvas.width) return 0
      const ctx = canvas.getContext('2d', { willReadFrequently: true })
      ctx.drawImage(video, 0, 0)
      const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data
      let h = 0
      for (let i = 0; i < data.length; i += 997) h = (h * 31 + data[i]) >>> 0
      return h
    }), handles)
    const states = []
    for (const l of labels) states.push(await probe(l))
    rows.push({ sec, counts, hashes, live: states.map((x) => x.live && !x.stale) })
  }
  return rows
}

let clicks = 0
async function connect(label) {
  clicks += 1
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
      const gate = await connectGated(label)
      const s = await sample(label, 6)
      out.cards[key] = { ...s, gate, live: true, decoded_changing: s.frames > 30 && s.width > 0 && s.distinct >= 3 }
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
    // 严格自动重连：本阶段绝不点击「连接视频」；30s 内未自行恢复即判失败（recovered=none）
    let recovered = 'none'
    const tRestore = Date.now()
    try {
      await card(label).locator('.video-live-tag').waitFor({ timeout: 30000 })
      recovered = 'auto'
    } catch {
      recovered = 'none'
    }
    out.recover_ms_after_restore = recovered === 'auto' ? Date.now() - tRestore : null
    out.clicks_during_reconnect = clicks
    const after = recovered === 'auto' ? await sample(label, 4) : { frames: 0, distinct: 0 }
    out.cards.rgb = { before, after, recovered, decoded_after: after.frames > 20 && after.distinct >= 2 }
    await card(label).screenshot({ path: path.join(outDir, 'videocard-rgb-reconnected.png') })
  }
  if (phase === 'dual60') {
    const labels = ['监控相机', '红外热成像']
    out.gates = []
    for (const l of labels) out.gates.push(await connectGated(l))
    const rows = await dualSample(labels, Number(input.seconds || 60))
    out.dual = rows
    const stats = labels.map((l, i) => {
      const deltas = rows.map((r, k) => r.counts[i] - (k ? rows[k - 1].counts[i] : 0))
      return {
        label: l,
        total_frames: rows.at(-1).counts[i],
        min_fps_1s: Math.min(...deltas),
        zero_fps_seconds: deltas.filter((d) => d === 0).length,
        distinct_hashes: new Set(rows.map((r) => r.hashes[i])).size,
        non_live_seconds: rows.filter((r) => !r.live[i]).length,
      }
    })
    out.dual_stats = stats
    await page.screenshot({ path: path.join(outDir, 'dual60-end.png') })
  } else if (phase === 'manual') {
    // 长时间断流：有界自动重连耗尽 → 明确“自动重连失败，请手动重试”；源恢复后不得自行恢复；
    // 手动点击「连接视频」后恢复解码
    const label = '监控相机'
    await connect(label)
    fs.writeFileSync(input.ready_file, 'connected')
    const t0 = Date.now()
    let exhausted = null
    while (Date.now() - t0 < 90000) {
      const p = await probe(label)
      if (/自动重连失败/.test(p.text)) { exhausted = { t: Date.now() - t0, text: p.text }; break }
      await page.waitForTimeout(500)
    }
    out.exhausted = exhausted
    fs.writeFileSync(input.exhausted_file, exhausted ? 'yes' : 'no')
    const deadline = Date.now() + 60000
    while (!fs.existsSync(input.restored_file) && Date.now() < deadline) await page.waitForTimeout(300)
    await page.waitForTimeout(6000) // 源已恢复 6s：不得无点击自行变为实时
    out.auto_after_exhausted = (await probe(label)).live
    out.clicks_before_manual = clicks
    await connect(label)
    const after = await sample(label, 4)
    out.manual = { clicks: clicks, after, decoded_after: after.frames > 20 && after.distinct >= 2 }
    await card(label).screenshot({ path: path.join(outDir, 'videocard-rgb-manual-recovered.png') })
  } else if (phase === 'switch') {
    // 模拟车辆双路播放 → 经设备页「切换监控」切到真实车辆 → 旧会话 DELETE，真实车辆页不出现模拟流/横幅
    for (const l of ['监控相机', '红外热成像']) await connect(l)
    out.before_switch = { banner: await page.locator('[data-testid="simulated-source-banner"]').count() }
    const before = requests.length
    // SPA 内导航（vue-router 接管，不整页重载）——验证组件卸载时真实释放旧 WHEP 会话
    await page.evaluate(() => {
      history.pushState({}, '', '/robots')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    await page.waitForSelector('.vehicle-row', { timeout: 30000 })
    out.navigation = 'spa'
    const row = page.locator('.vehicle-row', { hasText: input.target }).first()
    await row.getByRole('button', { name: /切换监控|进入实时监控/ }).first().click()
    await page.waitForURL(/\/monitor/, { timeout: 30000 })
    await page.waitForSelector('.video-surveillance .hmi-camera', { timeout: 30000 })
    await page.waitForTimeout(1500)
    const after = requests.slice(before)
    out.after_switch = {
      stored_vehicle: await page.evaluate(() => localStorage.getItem('firebot.activeVehicleId')),
      banner: await page.locator('[data-testid="simulated-source-banner"]').count(),
      media_deletes: after.filter((r) => r.startsWith('DELETE /media/')).length,
      // 切车后除释放旧会话的 DELETE 外，不得再有任何模拟流请求（票据 / WHEP POST）
      sim_requests_after: after.filter((r) => r.includes('firebot-sim-') && !r.startsWith('DELETE ')).length,
      sim_deletes: after.filter((r) => r.startsWith('DELETE /media/firebot-sim-')).length,
      cards: [],
    }
    for (const l of ['监控相机', '红外热成像']) {
      const c = card(l)
      out.after_switch.cards.push({
        label: l,
        live: (await c.locator('.video-live-tag').count()) > 0,
        video_src: await c.locator('video').evaluate((v) => Boolean(v.srcObject)),
        text: ((await c.locator('.video-placeholder').textContent().catch(() => '')) || '').trim(),
      })
    }
    out.page_text_has_sim = (await page.locator('main, body').first().textContent()).includes('firebot-sim-')
    await page.screenshot({ path: path.join(outDir, 'vehicle-switch-real.png') })
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
