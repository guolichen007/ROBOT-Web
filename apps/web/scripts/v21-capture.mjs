/* global process, console */
// Firebot V2.1 — real-browser screenshot capture (isolated SIM stack only).
// usage: node scripts/v21-capture.mjs <outDir> [label]
// env: CAPTURE_BASE_URL (default http://127.0.0.1:4173), CHROME_PATH, CAPTURE_ROUTES (comma list)
// Refuses to run against anything that is not loopback, so it can never hit production.
import { chromium } from '@playwright/test'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const base = process.env.CAPTURE_BASE_URL || 'http://127.0.0.1:4173'
if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(base)) {
  console.error(`REFUSED: capture base must be loopback, got ${base}`)
  process.exit(2)
}
const outDir = process.argv[2] || '/tmp/shots'
const label = process.argv[3] || 'baseline'
const routes = (process.env.CAPTURE_ROUTES || '/monitor,/maps,/parking,/patrol,/tasks,/alarms').split(',')
const sizes = [
  [1366, 768],
  [1920, 1080],
]
const passwords = ['Firebot-E2E-Changed-2026!', process.env.BOOTSTRAP_ADMIN_PASSWORD].filter(Boolean)
mkdirSync(outDir, { recursive: true })

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, args: ['--no-sandbox'] })
const manifest = []
for (const [w, h] of sizes) {
  const context = await browser.newContext({
    baseURL: base,
    viewport: { width: w, height: h },
    locale: 'zh-CN',
  })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(String(e)))
  // login through the real API (cookies shared with the page)
  let ok = false
  for (const pw of passwords) {
    const r = await page.request.post('/api/v1/auth/login', { data: { username: 'admin', password: pw } })
    if (!r.ok()) continue
    const body = await r.json()
    if (body.user.must_change_password) {
      const csrf = (await context.cookies()).find((c) => c.name === 'csrf_token')?.value || ''
      const c = await page.request.post('/api/v1/auth/change-password', {
        data: { current_password: pw, new_password: 'Firebot-E2E-Changed-2026!' },
        headers: { Authorization: `Bearer ${body.access_token}`, 'X-CSRF-Token': csrf },
      })
      if (!c.ok()) throw new Error(`change-password failed ${c.status()}`)
      continue
    }
    ok = true
    break
  }
  if (!ok) {
    // retry once with changed password after a forced change
    const r = await page.request.post('/api/v1/auth/login', {
      data: { username: 'admin', password: 'Firebot-E2E-Changed-2026!' },
    })
    if (!r.ok()) throw new Error(`login failed ${r.status()}`)
  }
  for (const route of routes) {
    await page.goto(route, { waitUntil: 'networkidle', timeout: 30_000 }).catch(() => {})
    await page.waitForTimeout(2500)
    const name = `${label}_${route.replace(/\//g, '') || 'root'}_${w}x${h}.png`
    await page.screenshot({ path: join(outDir, name), fullPage: false })
    manifest.push({ route, viewport: `${w}x${h}`, file: name, url: page.url(), pageErrors: [...errors] })
    errors.length = 0
  }
  await context.close()
}
await browser.close()
writeFileSync(join(outDir, `${label}_manifest.json`), JSON.stringify(manifest, null, 2))
console.log(JSON.stringify(manifest.map((m) => [m.file, m.url, m.pageErrors.length])))
