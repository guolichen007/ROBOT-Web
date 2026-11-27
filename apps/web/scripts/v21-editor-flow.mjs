/* global process, console, Buffer, Event */
// Firebot V2.1 WEB-2 — scene editor real-browser flow with hard assertions.
// usage: node scripts/v21-editor-flow.mjs <outDir>
// Loopback only. Generates a SYNTHETIC SIM map (not R002 field data, not DEMO_PARKING).
import { chromium } from '@playwright/test'
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const base = process.env.CAPTURE_BASE_URL || 'http://127.0.0.1:4173'
if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(base)) {
  console.error(`REFUSED: base must be loopback, got ${base}`)
  process.exit(2)
}
const out = process.argv[2] || '/tmp/editor-flow'
mkdirSync(out, { recursive: true })
const fx = join(out, 'fixture')
mkdirSync(fx, { recursive: true })

// 400x300 px, 0.05 m/px -> 20 m x 15 m, origin (-10,-5). Border walls, free inside.
const W = 400
const H = 300
const header = Buffer.from(`P5\n# firebot SIM synthetic\n${W} ${H}\n255\n`)
const px = Buffer.alloc(W * H, 254)
for (let r = 0; r < H; r++)
  for (let c = 0; c < W; c++) if (r < 4 || c < 4 || r >= H - 4 || c >= W - 4) px[r * W + c] = 0
writeFileSync(join(fx, 'sim_r002_gz.pgm'), Buffer.concat([header, px]))
const yaml = 'image: sim_r002_gz.pgm\nresolution: 0.05\norigin: [-10.0, -5.0, 0.0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n'
writeFileSync(join(fx, 'sim_r002_gz.yaml'), yaml)
writeFileSync(join(fx, 'evil.yaml'), yaml.replace('sim_r002_gz.pgm', '../sim_r002_gz.pgm'))

const results = []
const check = (name, fn) => {
  fn()
  results.push(name)
}

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, args: ['--no-sandbox'] })
for (const [w, h] of [
  [1366, 768],
  [1920, 1080],
]) {
  const ctx = await browser.newContext({ baseURL: base, viewport: { width: w, height: h }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  const pageErrors = []
  page.on('pageerror', (e) => pageErrors.push(String(e)))
  const writes = []
  page.on('request', (r) => {
    if (r.method() !== 'GET' && /\/api\/v1\/(robots|tasks|commands|map-versions|parking-slots|inspection-points|alarms)/.test(r.url()))
      writes.push(`${r.method()} ${r.url()}`)
  })
  let login
  for (const pw of ['Firebot-E2E-Changed-2026!', process.env.BOOTSTRAP_ADMIN_PASSWORD].filter(Boolean)) {
    login = await page.request.post('/api/v1/auth/login', { data: { username: 'admin', password: pw } })
    if (login.ok()) break
  }
  assert.equal(login.status(), 200, 'login')
  await page.goto('/maps/editor', { waitUntil: 'networkidle' })
  await page.getByTestId('scene-editor').waitFor()
  const tag = `${w}x${h}`

  // negative: traversal path in YAML is rejected, editor not opened
  await page.getByTestId('se-import').setInputFiles([join(fx, 'evil.yaml'), join(fx, 'sim_r002_gz.pgm')])
  await page.getByTestId('se-error').waitFor()
  assert.match(await page.getByTestId('se-error').innerText(), /YAML_IMAGE_PATH_FORBIDDEN/)
  assert.equal(await page.getByTestId('se-canvas').count(), 0)
  check(`${tag} NEG_YAML_TRAVERSAL`, () => {})

  // import the real pair
  await page.getByTestId('se-import').setInputFiles([join(fx, 'sim_r002_gz.yaml'), join(fx, 'sim_r002_gz.pgm')])
  await page.getByTestId('se-canvas').waitFor()
  await page.getByText('400×300 px').waitFor()
  check(`${tag} IMPORT_META`, () => {})

  const svg = page.locator('[data-testid="se-canvas"] svg')
  const box = await svg.boundingBox()
  assert.ok(box && box.width > 300 && box.height > 300, 'canvas sized')
  const at = (fx_, fy_) => ({ x: box.x + box.width * fx_, y: box.y + box.height * fy_ })

  for (const [kind, fx_, fy_] of [
    ['RETURN_HOME', 0.3, 0.6],
    ['INSPECTION', 0.5, 0.5],
    ['INSPECTION', 0.7, 0.4],
  ]) {
    await page.getByTestId(`se-add-${kind}`).click()
    const p = at(fx_, fy_)
    await page.mouse.click(p.x, p.y)
  }
  await page.getByTestId('se-point-HOME-01').waitFor()
  await page.getByTestId('se-point-INSP-02').waitFor()
  await page.getByTestId('se-point-INSP-03').waitFor()
  check(`${tag} ADD_3_POINTS`, () => {})

  // precise input in world metres
  await page.locator('[data-testid="se-point-INSP-02"] circle').click()
  await page.getByTestId('se-x').fill('1.25')
  await page.getByTestId('se-x').press('Enter')
  await page.getByTestId('se-y').fill('-0.5')
  await page.getByTestId('se-y').press('Enter')
  await page.getByTestId('se-yaw').fill('90')
  await page.getByTestId('se-yaw').press('Enter')
  assert.equal(await page.getByTestId('se-x').inputValue(), '1.25')
  assert.equal(await page.getByTestId('se-yaw').inputValue(), '90.0')
  check(`${tag} PRECISE_INPUT`, () => {})

  // drag INSP-03 -> one undo step
  const g = await page.locator('[data-testid="se-point-INSP-03"] circle').boundingBox()
  const from = { x: g.x + g.width / 2, y: g.y + g.height / 2 }
  const yBefore = await page.getByTestId('se-point-INSP-03').getAttribute('transform')
  await page.mouse.move(from.x, from.y)
  await page.mouse.down()
  for (let i = 1; i <= 8; i++) await page.mouse.move(from.x + i * 10, from.y + i * 5)
  await page.mouse.up()
  const yAfter = await page.getByTestId('se-point-INSP-03').getAttribute('transform')
  assert.notEqual(yAfter, yBefore, 'drag moved the point')
  await page.getByTestId('se-undo').click()
  assert.equal(await page.getByTestId('se-point-INSP-03').getAttribute('transform'), yBefore, 'single undo restores drag')
  await page.getByTestId('se-redo').click()
  assert.equal(await page.getByTestId('se-point-INSP-03').getAttribute('transform'), yAfter)
  check(`${tag} DRAG_UNDO_REDO_ONE_STEP`, () => {})

  // route + delete referenced point refused
  await page.getByTestId('se-add-route').click()
  await page.getByText(/R-\d+ · 3 点/).waitFor()
  await page.locator('[data-testid="se-point-INSP-02"] circle').click()
  await page.getByTestId('se-delete').click()
  assert.match(await page.getByTestId('se-error').innerText(), /POINT_REFERENCED_BY_ROUTE/)
  await page.getByTestId('se-point-INSP-02').waitFor()
  check(`${tag} NEG_DELETE_REFERENCED_POINT`, () => {})

  // NaN-ish precise input refused (empty -> 0 is allowed; letters -> NaN)
  await page.getByTestId('se-x').evaluate((el) => {
    el.type = 'text'
    el.value = 'abc'
    el.dispatchEvent(new Event('change'))
  })
  assert.match(await page.getByTestId('se-error').innerText(), /NON_FINITE_NUMBER/)
  check(`${tag} NEG_NON_FINITE_INPUT`, () => {})

  // validation: traversability never auto-passes
  await page.getByTestId('se-validate').click()
  const issues = await page.getByTestId('se-issues').innerText()
  assert.match(issues, /可通行性待真实规划验证/)
  assert.doesNotMatch(issues, /缺少返航点/)
  check(`${tag} VALIDATE_PENDING_TRAVERSABILITY`, () => {})

  await page.locator('[data-testid="se-point-INSP-03"] circle').click()
  await page.screenshot({ path: join(out, `web2_editor_${tag}.png`) })

  assert.deepEqual(writes, [], 'editor must not write to server control/map endpoints')
  assert.deepEqual(pageErrors, [], 'no page errors')
  check(`${tag} ZERO_SERVER_WRITES`, () => {})
  await ctx.close()
}
await browser.close()
writeFileSync(join(out, 'editor-flow-result.json'), JSON.stringify({ passed: results }, null, 2))
console.log(`EDITOR_FLOW PASS ${results.length} assertions groups`)
