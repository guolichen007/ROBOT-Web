import { expect, type Page } from '@playwright/test'

// WEB-V2.1 approved console layout (DESIGN.md §3.1), replacing the pre-V2.1 three-column contract:
//   map dominates the left; video sits above the control rail on the right; fixed action bar spans
//   the bottom; stop / e-stop / protective-stop are always inside the viewport.
// Every assertion is geometric and checked at each viewport; nothing from the old contract
// (no overlap, aligned tops, in-viewport, no horizontal scroll, KPI row order) is dropped.
export const CONSOLE_VIEWPORTS = [
  { width: 1366, height: 768 },
  { width: 1680, height: 900 },
  { width: 1920, height: 1080 },
] as const

type Box = { x: number; y: number; width: number; height: number }
const box = async (page: Page, selector: string): Promise<Box> => {
  const b = await page.locator(selector).first().boundingBox()
  expect(b, `${selector} must be rendered`).not.toBeNull()
  return b!
}
const disjoint = (a: Box, b: Box) =>
  a.x + a.width <= b.x + 1 ||
  b.x + b.width <= a.x + 1 ||
  a.y + a.height <= b.y + 1 ||
  b.y + b.height <= a.y + 1

export async function assertConsoleLayout(page: Page, viewport: { width: number; height: number }) {
  const tag = `${viewport.width}x${viewport.height}`
  const map = await box(page, '.operations-map-panel')
  const video = await box(page, '.video-surveillance')
  const rail = await box(page, '.hmi-control-rail')
  const bar = await box(page, '[data-testid="console-action-bar"]')
  const protective = await box(page, '[data-testid="protective-stop"]')
  const status = await box(page, '[data-testid="console-status"]')

  // 1. map dominates: left column, wider than the right column and larger area than video
  expect(map.x + map.width, `${tag} map left of video`).toBeLessThanOrEqual(video.x + 1)
  expect(map.x + map.width, `${tag} map left of rail`).toBeLessThanOrEqual(rail.x + 1)
  expect(map.width, `${tag} map wider than right column`).toBeGreaterThan(video.width * 1.4)
  expect(map.width * map.height, `${tag} map area > video area`).toBeGreaterThan(video.width * video.height)
  expect(map.height, `${tag} map min height`).toBeGreaterThanOrEqual(viewport.height >= 900 ? 420 : 300)
  // 2. right column: video stacked above control rail, same column, no overlap
  expect(Math.abs(video.x - rail.x), `${tag} video/rail share column`).toBeLessThan(2)
  expect(video.y + video.height, `${tag} video above rail`).toBeLessThanOrEqual(rail.y + 1)
  expect(Math.abs(map.y - video.y), `${tag} map/video tops aligned`).toBeLessThan(2)
  expect(Math.abs(map.y + map.height - (rail.y + rail.height)), `${tag} bottoms aligned`).toBeLessThan(2)
  // 3. both video panes visible side by side inside the video panel
  const cams = await page.locator('.hmi-camera').all()
  expect(cams.length).toBe(2)
  const [c0, c1] = [(await cams[0].boundingBox())!, (await cams[1].boundingBox())!]
  expect(c0.x + c0.width, `${tag} cameras side by side`).toBeLessThanOrEqual(c1.x + 1)
  for (const c of [c0, c1]) {
    expect(c.height, `${tag} camera visible`).toBeGreaterThan(80)
    expect(c.y + c.height).toBeLessThanOrEqual(video.y + video.height + 1)
  }
  // 4. status bar on top, fixed action bar below the core, everything inside the viewport
  expect(status.y + status.height, `${tag} status above map`).toBeLessThanOrEqual(map.y + 1)
  expect(bar.y, `${tag} action bar below map/rail`).toBeGreaterThanOrEqual(
    Math.max(map.y + map.height, rail.y + rail.height) - 1,
  )
  for (const [name, b] of [
    ['rail', rail],
    ['bar', bar],
    ['protective', protective],
  ] as const) {
    expect(b.x + b.width, `${tag} ${name} inside width`).toBeLessThanOrEqual(viewport.width + 1)
    expect(b.y + b.height, `${tag} ${name} inside height`).toBeLessThanOrEqual(viewport.height + 1)
  }
  // 5. stop / e-stop / protective-stop visible and unobstructed (topmost element at their centre)
  const dock = page.locator('.operations-command-dock')
  for (const name of [/停止|已停止|停止中/, /软件急停|解除急停/]) {
    await expect(dock.getByRole('button', { name }).first()).toBeVisible()
  }
  await expect(page.locator('button.protective-stop')).toBeVisible()
  const hit = await page.evaluate(() =>
    ['.hold-estop', 'button.protective-stop', '.hmi-control-tabs button'].map((sel) => {
      const el = document.querySelector(sel) as HTMLElement | null
      if (!el) return false
      const r = el.getBoundingClientRect()
      const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)
      return Boolean(top && (top === el || el.contains(top)))
    }),
  )
  expect(hit, `${tag} e-stop / protective stop / rail tabs not covered`).toEqual([true, true, true])
  // a fire popup (if any) must not overlap the control rail or the fixed action area
  const popup = page.getByTestId('alarm-popup')
  if (await popup.count()) {
    const p = (await popup.boundingBox())!
    expect(disjoint(p, rail), `${tag} alarm popup over control rail`).toBe(true)
    expect(disjoint(p, bar), `${tag} alarm popup over action bar`).toBe(true)
  }
  // 6. no region overlaps another, no horizontal scroll, KPI row stays one ordered row
  const regions = [map, video, rail, bar, status]
  for (let i = 0; i < regions.length; i++)
    for (let j = i + 1; j < regions.length; j++)
      expect(disjoint(regions[i], regions[j]), `${tag} regions ${i}/${j} overlap`).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width)
  const kpis = await page.locator('.hmi-kpi').all()
  expect(kpis.length).toBe(4)
  for (let i = 1; i < kpis.length; i++) {
    const previous = (await kpis[i - 1].boundingBox())!
    const current = (await kpis[i].boundingBox())!
    expect(previous.x + previous.width).toBeLessThanOrEqual(current.x)
    expect(Math.abs(previous.y - current.y)).toBeLessThan(2)
  }
}
