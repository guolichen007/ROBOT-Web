/* global process, console, document */
// Dumps console region geometry at each viewport (evidence for layout thresholds).
import { chromium } from '@playwright/test'
import { writeFileSync } from 'node:fs'
import { login } from './r1c-login.mjs'
const b = await chromium.launch({ executablePath: process.env.CHROME_PATH, args: ['--no-sandbox'] })
const ctx = await b.newContext({ baseURL: process.env.CAPTURE_BASE_URL, locale: 'zh-CN' })
const p = await ctx.newPage()
await login(ctx, p)
const out = {}
for (const [w, h] of [
  [1366, 768],
  [1680, 900],
  [1920, 1080],
]) {
  await p.setViewportSize({ width: w, height: h })
  await p.goto('/monitor', { waitUntil: 'networkidle' })
  await p.waitForTimeout(1500)
  out[`${w}x${h}`] = await p.evaluate(() => {
    const r = (s) => {
      const e = document.querySelector(s)
      if (!e) return null
      const b = e.getBoundingClientRect()
      return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]
    }
    return {
      panel: r('[data-testid="console-control-panel"]'),
      map: r('.operations-map-panel'),
      video: r('.video-surveillance'),
      cam0: r('.hmi-camera'),
      bar: r('[data-testid="console-action-bar"]'),
      status: r('[data-testid="console-status"]'),
      alert: r('#workspace-alert'),
      topbar: r('.topbar'),
    }
  })
}
console.log(JSON.stringify(out))
writeFileSync(process.argv[2], JSON.stringify(out, null, 1))
await b.close()
