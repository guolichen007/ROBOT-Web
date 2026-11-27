/* global process, console */
import { chromium } from '@playwright/test'
import { login } from './r1c-login.mjs'
const base = process.env.CAPTURE_BASE_URL
const out = process.argv[2]
const sizes = (process.env.SIZES || '1366x768,1680x900,1920x1080')
  .split(',')
  .map((s) => s.split('x').map(Number))
const b = await chromium.launch({ executablePath: process.env.CHROME_PATH, args: ['--no-sandbox'] })
const ctx = await b.newContext({ baseURL: base, locale: 'zh-CN' })
const p = await ctx.newPage()
const errs = []
p.on('pageerror', (e) => errs.push(String(e)))
await login(ctx, p)
console.log('login ok')
for (const [w, h] of sizes) {
  await p.setViewportSize({ width: w, height: h })
  await p.goto('/monitor', { waitUntil: 'networkidle' })
  await p.waitForTimeout(2500)
  await p.screenshot({ path: `${out}/peek_${w}x${h}.png` })
}
console.log('errors', JSON.stringify(errs))
await b.close()
