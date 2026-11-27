/* global process, console, Buffer, document */
// WEB-R1C R1C-3 scene editor real-browser business loop (loopback isolated stack only).
// import -> place points -> route -> mark an INTERMEDIATE waypoint STOP -> save -> reload (arrival
// survives) -> slot + corridor -> save -> checksum -> atomic publish -> list shows PUBLISHED ->
// reopen from list. Screenshots at 1366x768 / 1680x900 / 1920x1080.
// usage: node scripts/r1c-editor-flow.mjs <outDir>
import { chromium } from '@playwright/test'
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { login } from './r1c-login.mjs'

const base = process.env.CAPTURE_BASE_URL || ''
if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(base)) {
  console.error(`REFUSED: base must be loopback, got ${base}`)
  process.exit(2)
}
const out = process.argv[2] || '/tmp/r1c-editor'
const fx = join(out, 'fixture')
mkdirSync(fx, { recursive: true })
const W = 400
const H = 300
const px = Buffer.alloc(W * H, 254)
writeFileSync(join(fx, 'sim_r1c.pgm'), Buffer.concat([Buffer.from(`P5\n${W} ${H}\n255\n`), px]))
writeFileSync(
  join(fx, 'sim_r1c.yaml'),
  'image: sim_r1c.pgm\nresolution: 0.05\norigin: [-10.0, -5.0, 0.0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n',
)
const steps = []
const ok = (name, extra = {}) => {
  steps.push({ name, ...extra })
  console.log('PASS', name)
}

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, args: ['--no-sandbox'] })
const ctx = await browser.newContext({
  baseURL: base,
  viewport: { width: 1920, height: 1080 },
  locale: 'zh-CN',
})
const page = await ctx.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))
await login(ctx, page)
await page.goto('/maps/editor')
await page.getByTestId('se-scene-list').waitFor()
ok('scene_list_visible')
await page.getByTestId('se-import').setInputFiles([join(fx, 'sim_r1c.yaml'), join(fx, 'sim_r1c.pgm')])
await page.getByTestId('se-canvas').waitFor()
await page.waitForFunction(() =>
  document.querySelector('[data-testid="se-persist"]')?.textContent?.includes('rev'),
)
ok('import_server_draft')

const canvas = page.locator('[data-testid="se-canvas"] svg')
const box = await canvas.boundingBox()
async function place(kind, fx2, fy2) {
  await page.getByTestId(`se-add-${kind}`).click()
  await canvas.click({ position: { x: box.width * fx2, y: box.height * fy2 } })
}
await place('RETURN_HOME', 0.2, 0.7)
for (const [x, y] of [
  [0.35, 0.5],
  [0.5, 0.5],
  [0.65, 0.5],
])
  await place('INSPECTION', x, y)
await page.getByTestId('se-add-route').click()
const route = page.locator('[data-testid^="se-route-"]').first()
await route.waitFor()
const arrivals = async () => (await route.locator('button').allTextContents()).map((t) => t.trim())
assert.deepEqual(await arrivals(), ['经过', '经过', '停靠'])
await route.locator('button').nth(1).click() // intermediate waypoint -> STOP
assert.deepEqual(await arrivals(), ['经过', '停靠', '停靠'])
ok('intermediate_stop_marked')
await page.getByTestId('se-save').click()
await page.waitForFunction(
  () => !document.querySelector('[data-testid="se-save"]')?.textContent?.includes('保存中'),
)
await page.waitForTimeout(500)
await page.reload()
await page.locator('[data-testid^="se-route-"]').first().waitFor()
assert.deepEqual(
  (await page.locator('[data-testid^="se-route-"]').first().locator('button').allTextContents()).map((t) =>
    t.trim(),
  ),
  ['经过', '停靠', '停靠'],
  'intermediate STOP must survive save + reload',
)
ok('reload_keeps_intermediate_stop')

// slot from a selected point + corridor along inspection points
await page.locator('.se-tree .se-group').first().locator('button.se-item').nth(1).click()
await page.getByTestId('se-add-slot').click()
await page.getByTestId('se-add-corridor').click()
await page.getByTestId('se-save').click()
await page.waitForTimeout(800)
await page.reload()
await page.locator('[data-testid^="se-route-"]').first().waitFor()
const tree = await page.locator('.se-tree').innerText()
assert.match(tree, /车位（1）/)
assert.match(tree, /右车道走廊（1）/)
ok('slot_and_corridor_persisted')

await page.getByTestId('se-checksum-input').fill('sim-r1c-map-v1')
await page.getByTestId('se-checksum-save').click()
await page.waitForTimeout(600)
await page.getByTestId('se-publish').click()
await page.waitForFunction(() => document.querySelector('.se-status')?.textContent?.includes('PUBLISHED'))
const persist = await page.getByTestId('se-persist').innerText()
assert.match(persist, /摘要 [0-9a-f]{12}/)
assert.equal(await page.getByTestId('se-save').isDisabled(), true, 'published scene is read-only')
ok('atomic_publish', { persist })

for (const [w, h] of [
  [1366, 768],
  [1680, 900],
  [1920, 1080],
]) {
  await page.setViewportSize({ width: w, height: h })
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(out, `r1c_editor_published_${w}x${h}.png`) })
}
await page.goto('/maps/editor')
const list = page.getByTestId('se-scene-list')
await list.waitFor()
const listText = await list.innerText()
assert.match(listText, /PUBLISHED/)
ok('list_shows_published')
await list.locator('button').first().click()
await page.locator('[data-testid^="se-route-"]').first().waitFor()
ok('reopen_from_list')
assert.deepEqual(errors, [])
writeFileSync(join(out, 'r1c-editor-flow.json'), JSON.stringify({ steps }, null, 2))
console.log(`R1C_EDITOR_FLOW PASS ${steps.length}`)
await browser.close()
