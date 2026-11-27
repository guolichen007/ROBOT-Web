/* global process, console, document */
// Firebot WEB-R1A — comprehensive console real-browser flow (isolated SIM stack only).
// usage: node scripts/v21-console-flow.mjs <outDir>
// States are produced by the REAL stack (API + PostgreSQL + Redis + mosquitto + ingress +
// dispatcher + task-worker + mock-robot), never by front-end stubs:
//   normal  -> idle robot streaming telemetry
//   task    -> return-dock task created through the API (patrol readiness permitting)
//   fault   -> software e-stop through the API (latched by the mock robot, ack'ed)
//   offline -> the whole vehicle side stops publishing (process killed by the caller)
// Each state: assert geometry at 1366x768 and 1920x1080, screenshot, record state JSON.
import { chromium } from '@playwright/test'
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const base = process.env.CAPTURE_BASE_URL || 'http://127.0.0.1:4173'
if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(base)) {
  console.error(`REFUSED: base must be loopback, got ${base}`)
  process.exit(2)
}
const out = process.argv[2] || '/tmp/console-flow'
const only = (process.env.CONSOLE_STATES || 'normal,task,fault').split(',')
mkdirSync(out, { recursive: true })
const VIEWPORTS = [
  [1366, 768],
  [1920, 1080],
]
const results = []
const states = []

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, args: ['--no-sandbox'] })
const ctx = await browser.newContext({
  baseURL: base,
  viewport: { width: 1366, height: 768 },
  locale: 'zh-CN',
})
const page = await ctx.newPage()
const pageErrors = []
page.on('pageerror', (e) => pageErrors.push(String(e)))
let token = ''
for (const pw of ['Firebot-E2E-Changed-2026!', process.env.BOOTSTRAP_ADMIN_PASSWORD].filter(Boolean)) {
  const r = await page.request.post('/api/v1/auth/login', { data: { username: 'admin', password: pw } })
  if (r.ok()) {
    token = (await r.json()).access_token
    break
  }
}
assert.ok(token, 'login')
const csrf = async () => (await ctx.cookies()).find((c) => c.name === 'csrf_token')?.value || ''
const api = async (method, path, data) => {
  const r = await page.request.fetch(`/api/v1${path}`, {
    method,
    data,
    headers: {
      Authorization: `Bearer ${token}`,
      'X-CSRF-Token': await csrf(),
      'Idempotency-Key': `r1a-${Date.now()}-${Math.random()}`,
    },
  })
  return { status: r.status(), body: await r.json().catch(() => null) }
}
const robotState = async () => {
  const snap = (await api('GET', '/monitor/snapshot')).body
  const r = snap.robots.find((x) => x.vehicle_id === 'R001')
  return {
    online_state: r?.online_state,
    estop_active: r?.estop_active,
    boot_id: r?.boot_id,
    x: r?.x,
    y: r?.y,
    theta: r?.theta,
    server_received_at: r?.server_received_at,
    safety_command_ready: r?.safety_command_ready,
    autonomous_task_ready: r?.autonomous_task_ready,
    manual_control_ready: r?.manual_control_ready,
    active_tasks: snap.tasks
      .filter((t) => t.robot_id === r?.id && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(t.status))
      .map((t) => ({ type: t.type, status: t.status, progress: t.progress })),
  }
}

async function robotRowId() {
  const snap = (await api('GET', '/monitor/snapshot')).body
  return snap.robots.find((x) => x.vehicle_id === 'R001')?.id
}

async function geometry(tag, w, h, expect) {
  await page.setViewportSize({ width: w, height: h })
  await page.goto('/monitor', { waitUntil: 'networkidle' })
  await page.getByTestId('console-status').waitFor()
  await page.waitForTimeout(1800)
  const bar = await page.getByTestId('console-action-bar').boundingBox()
  const prot = await page.getByTestId('protective-stop').boundingBox()
  const map = await page.locator('.operations-map-panel').boundingBox()
  const video = await page.locator('.video-surveillance').boundingBox()
  const cams = await page.locator('.hmi-camera').count()
  const scrollW = await page.evaluate(() => document.documentElement.scrollWidth)
  const dockButtons = await page.locator('.operations-command-dock').getByRole('button').allTextContents()
  // stop / estop / protective stop visible inside the viewport, no horizontal scroll
  assert.ok(bar && bar.y + bar.height <= h + 1, `${tag} action bar inside viewport`)
  assert.ok(
    prot && prot.y + prot.height <= h + 1 && prot.x + prot.width <= w + 1,
    `${tag} protective stop visible`,
  )
  assert.ok(scrollW <= w + 2, `${tag} no horizontal scroll`)
  assert.equal(cams, 2, `${tag} two video panes`)
  assert.ok(map && video && map.width > video.width, `${tag} map dominates`)
  assert.ok(map.height >= 250, `${tag} map height ${map.height}`)
  assert.ok(
    dockButtons.some((t) => /停止/.test(t)),
    `${tag} stop entry`,
  )
  assert.ok(
    dockButtons.some((t) => /软件急停|解除急停/.test(t)),
    `${tag} estop entry`,
  )
  // fire popup (if present) must not overlap the fixed action area
  const popup = page.getByTestId('alarm-popup')
  let popupOverlap = null
  if (await popup.count()) {
    const p = await popup.boundingBox()
    popupOverlap = !(
      p.y + p.height <= bar.y ||
      p.y >= bar.y + bar.height ||
      p.x + p.width <= bar.x ||
      p.x >= bar.x + bar.width
    )
    assert.equal(popupOverlap, false, `${tag} alarm popup overlaps the action bar`)
  }
  // protective stop never pretends to be available
  assert.equal(
    await page.locator('button.protective-stop').isDisabled(),
    true,
    `${tag} protective stop disabled`,
  )
  assert.match(await page.getByTestId('protective-stop').innerText(), /未实现|未接入|不可用/)
  for (const [k, v] of Object.entries(expect)) {
    const text = await page.locator(`[data-dim="${k}"]`).innerText()
    assert.match(text, v, `${tag} dim ${k}: ${text}`)
  }
  const file = `r1a_console_${tag}_${w}x${h}.png`
  await page.screenshot({ path: join(out, file) })
  results.push(`${tag}_${w}x${h}`)
  return {
    viewport: `${w}x${h}`,
    file,
    map: map && { w: Math.round(map.width), h: Math.round(map.height) },
    action_bar_bottom: Math.round(bar.y + bar.height),
    protective_stop: Math.round(prot.y + prot.height),
    popup_overlap: popupOverlap,
    status: await page.getByTestId('console-status').innerText(),
  }
}

async function stateRun(tag, expect) {
  const rec = { state: tag, server: await robotState(), shots: [] }
  for (const [w, h] of VIEWPORTS) rec.shots.push(await geometry(tag, w, h, expect))
  states.push(rec)
}

async function waitFor(fn, label, ms = 20000) {
  const end = Date.now() + ms
  while (Date.now() < end) {
    if (await fn()) return
    await page.waitForTimeout(500)
  }
  throw new Error(`timeout: ${label}`)
}

await waitFor(async () => (await robotState()).online_state === 'ONLINE', 'R001 online')
await page.goto('/monitor', { waitUntil: 'networkidle' })
if ((await page.getByTestId('console-vehicle').inputValue()) !== 'R001') {
  await page.getByTestId('console-vehicle').selectOption('R001')
}

if (only.includes('normal')) await stateRun('normal', { link: /实时/, device: /在线/, task: /空闲/ })

if (only.includes('task')) {
  // a patrol (legacy executable plan in the test seed) runs long enough to observe; return-dock
  // from the waiting area completes almost instantly and would legitimately show 空闲 again.
  const plans = (await api('GET', '/patrol-plans')).body || []
  const rid = await robotRowId()
  const plan = plans.find((p) => p.robot_id === rid && p.execution_mode !== 'PREVIEW_ONLY')
  const created = plan
    ? await api('POST', '/tasks/patrol-plan', { robot_id: 'R001', patrol_plan_id: plan.id })
    : { status: 0, body: 'no executable patrol plan in seed' }
  results.push(`task_create_http_${created.status}`)
  if (created.status < 300) {
    await waitFor(async () => (await robotState()).active_tasks.length > 0, 'active task visible')
    await stateRun('task', { link: /实时/, task: /巡检|返航|导航/ })
    const cancel = (await robotState()).active_tasks.length
    results.push(`task_active_after_capture_${cancel}`)
  } else {
    states.push({ state: 'task', skipped: true, reason: created.body })
  }
}

if (only.includes('fault')) {
  const estop = await api('POST', '/robots/R001/commands/emergency-stop', {})
  results.push(`estop_http_${estop.status}`)
  await waitFor(async () => (await robotState()).estop_active === true, 'estop latched by vehicle', 15000)
  await stateRun('fault', { safety: /软件急停锁存/ })
  states[states.length - 1].estop_command = estop.body && {
    lifecycle: estop.body.lifecycle_status,
    cmd: estop.body.cmd,
  }
}

if (only.includes('offline')) {
  await waitFor(
    async () => ['OFFLINE', 'STALE'].includes((await robotState()).online_state),
    'vehicle offline',
    30000,
  )
  await page.waitForTimeout(11000)
  await stateRun('offline', { link: /离线|陈旧|中断/, device: /离线|陈旧/ })
  const posed = await page.getByTestId('console-pose').innerText()
  assert.match(posed, /未知|旧数据/, 'offline pose must be unknown or marked old')
}

assert.deepEqual(pageErrors, [], 'no page errors')
await browser.close()
writeFileSync(
  join(out, `console-states-${only.join('-')}.json`),
  JSON.stringify({ results, states }, null, 2),
)
console.log(`CONSOLE_FLOW PASS ${results.length} :: ${results.join(' ')}`)
