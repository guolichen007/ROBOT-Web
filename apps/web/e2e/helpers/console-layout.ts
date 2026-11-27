import { expect, type Page } from '@playwright/test'

// WEB-R1C OWNER-approved console layout (DESIGN.md §3.1), replacing the R1A/R1B contract (videos
// side by side, control rail on the right — no longer what the OWNER wants):
//   left   vehicle control panel (设备 / 手动 / 监测) in the sidebar, BELOW the 系统设置 nav item
//   centre map dominates
//   right  ONLY the two videos, stacked: front monitoring camera on top, thermal below
//   bottom fixed task bar (巡检 · 暂停/继续 · 目标导航 · 返航) + always-visible safety group
//          (停止 · 软件急停 · 保护停车)
// Every assertion is geometric and checked at each viewport. Nothing from the previous contract
// that still applies is dropped (no overlap, in-viewport, no horizontal scroll, KPI row order,
// unobstructed safety, alarm popup clear of the operating areas).
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
const inside = (inner: Box, outer: Box) =>
  inner.x >= outer.x - 1 &&
  inner.y >= outer.y - 1 &&
  inner.x + inner.width <= outer.x + outer.width + 1 &&
  inner.y + inner.height <= outer.y + outer.height + 1

/** Selectors whose centre must be the topmost element (nothing covers them). */
export const UNOBSTRUCTED = [
  '[data-testid="console-safety-group"] .dock-stop',
  '.hold-estop',
  'button.protective-stop',
  '.console-panel-tabs button[data-tab="device"]',
  '.console-panel-tabs button[data-tab="manual"]',
  '.console-panel-tabs button[data-tab="monitor"]',
] as const

export async function assertConsoleLayout(page: Page, viewport: { width: number; height: number }) {
  const tag = `${viewport.width}x${viewport.height}`
  await expect(page.getByTestId('console-control-panel')).toBeVisible()
  const sidebar = await box(page, '.sidebar')
  const nav = await box(page, '.sidebar nav[aria-label="主导航"]')
  const settings = await box(page, '.sidebar nav[aria-label="主导航"] > :last-child')
  const panel = await box(page, '[data-testid="console-control-panel"]')
  const map = await box(page, '.operations-map-panel')
  const video = await box(page, '.video-surveillance')
  const bar = await box(page, '[data-testid="console-action-bar"]')
  const taskGroup = await box(page, '[data-testid="console-task-group"]')
  const safety = await box(page, '[data-testid="console-safety-group"]')
  const protective = await box(page, '[data-testid="protective-stop"]')
  const status = await box(page, '[data-testid="console-status"]')

  // 1. left control panel lives in the sidebar, directly below 系统设置 (the last nav item)
  await expect(page.locator('.sidebar nav[aria-label="主导航"] > :last-child')).toContainText('系统设置')
  expect(inside(panel, sidebar), `${tag} control panel inside the sidebar`).toBe(true)
  expect(panel.y, `${tag} control panel below 系统设置`).toBeGreaterThanOrEqual(
    settings.y + settings.height - 1,
  )
  expect(panel.y, `${tag} control panel below the whole nav`).toBeGreaterThanOrEqual(nav.y + nav.height - 1)
  expect(panel.height, `${tag} control panel usable height`).toBeGreaterThanOrEqual(240)
  expect(panel.y + panel.height, `${tag} control panel inside viewport`).toBeLessThanOrEqual(
    viewport.height + 1,
  )
  const tabs = await page.locator('.console-panel-tabs button').allTextContents()
  expect(
    tabs.map((t) => t.trim()),
    `${tag} compact tabs`,
  ).toEqual(['设备', '手动', '监测'])
  // the old right-hand control rail and its tabs are gone
  await expect(page.locator('.hmi-control-rail')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '自动导航', exact: true })).toHaveCount(0)

  // 2. right column = ONLY the two videos, stacked vertically (camera on top, thermal below)
  const cams = page.locator('.video-surveillance .hmi-camera')
  await expect(cams).toHaveCount(2)
  await expect(cams.nth(0).locator('h3')).toHaveText('监控相机')
  await expect(cams.nth(1).locator('h3')).toHaveText('红外热成像')
  const [c0, c1] = [(await cams.nth(0).boundingBox())!, (await cams.nth(1).boundingBox())!]
  expect(c0.y + c0.height, `${tag} camera above thermal`).toBeLessThanOrEqual(c1.y + 1)
  expect(Math.abs(c0.x - c1.x), `${tag} videos share one column`).toBeLessThan(2)
  expect(Math.abs(c0.width - c1.width), `${tag} videos same width`).toBeLessThan(2)
  for (const [name, c] of [
    ['camera', c0],
    ['thermal', c1],
  ] as const) {
    expect(inside(c, video), `${tag} ${name} inside the video panel`).toBe(true)
    expect(c.height, `${tag} ${name} visible height`).toBeGreaterThan(120)
  }
  const sideChildren = await page.locator('.console-side > *').count()
  expect(sideChildren, `${tag} right column holds only the video panel`).toBe(1)
  // original aspect ratio: every video element letterboxes (contain), never stretches/crops,
  // and each stage stays a sane landscape shape (not a squeezed strip)
  const media = await page.evaluate(() =>
    [...document.querySelectorAll('.video-surveillance .video-stage')].map((stage) => {
      const r = stage.getBoundingClientRect()
      const v = stage.querySelector('video')
      return { ratio: r.width / Math.max(1, r.height), fit: v ? getComputedStyle(v).objectFit : 'contain' }
    }),
  )
  expect(media.length).toBe(2)
  for (const m of media) {
    expect(m.fit, `${tag} video object-fit`).toBe('contain')
    expect(m.ratio, `${tag} video stage ratio ${m.ratio.toFixed(2)}`).toBeGreaterThan(1.05)
    expect(m.ratio, `${tag} video stage ratio ${m.ratio.toFixed(2)}`).toBeLessThan(2.6)
  }

  // 3. map dominates the centre
  expect(map.x, `${tag} map right of the sidebar`).toBeGreaterThanOrEqual(sidebar.x + sidebar.width - 1)
  expect(map.x + map.width, `${tag} map left of videos`).toBeLessThanOrEqual(video.x + 1)
  expect(map.width, `${tag} map wider than video column`).toBeGreaterThan(video.width * 1.5)
  expect(map.width * map.height, `${tag} map area > video area`).toBeGreaterThan(
    video.width * video.height * 1.5,
  )
  expect(map.width * map.height, `${tag} map area > control panel area`).toBeGreaterThan(
    panel.width * panel.height,
  )
  // measured R1C geometry (evidence r1c_geometry.json): 826x436 / 976x550 / 1156x730 — thresholds
  // sit just below so any regression that shrinks the map fails
  const minMapHeight = viewport.height >= 1080 ? 640 : viewport.height >= 900 ? 520 : 400
  expect(map.height, `${tag} map min height`).toBeGreaterThanOrEqual(minMapHeight)
  expect(map.width / (map.width + video.width), `${tag} map share of the core width`).toBeGreaterThanOrEqual(
    0.66,
  )
  expect(Math.abs(map.y - video.y), `${tag} map/video tops aligned`).toBeLessThan(2)
  expect(
    Math.abs(map.y + map.height - (video.y + video.height)),
    `${tag} map/video bottoms aligned`,
  ).toBeLessThan(2)

  // 4. fixed task bar at the bottom: below map+video, inside the viewport, task + safety groups
  expect(bar.y, `${tag} task bar below map/video`).toBeGreaterThanOrEqual(
    Math.max(map.y + map.height, video.y + video.height) - 1,
  )
  expect(bar.y + bar.height, `${tag} task bar inside viewport`).toBeLessThanOrEqual(viewport.height + 1)
  expect(viewport.height - (bar.y + bar.height), `${tag} task bar pinned to the bottom`).toBeLessThan(24)
  expect(inside(taskGroup, bar) && inside(safety, bar), `${tag} groups inside bar`).toBe(true)
  expect(taskGroup.x + taskGroup.width, `${tag} task group left of safety group`).toBeLessThanOrEqual(
    safety.x + 1,
  )
  expect(inside(protective, safety), `${tag} protective stop inside the safety group`).toBe(true)
  const taskText = (await page.getByTestId('console-task-group').innerText()).replace(/\s+/g, ' ')
  const canonical =
    /巡检/.test(taskText) &&
    /暂停|继续/.test(taskText) &&
    /目标导航|前往/.test(taskText) &&
    /返回|已在等待区/.test(taskText)
  const agent =
    /开始巡逻/.test(taskText) &&
    /停止巡逻/.test(taskText) &&
    /恢复巡逻/.test(taskText) &&
    /前往目标/.test(taskText) &&
    /返航/.test(taskText)
  expect(canonical || agent, `${tag} task bar holds patrol / pause-resume / goal / return: ${taskText}`).toBe(
    true,
  )

  // 5. safety entries always visible and unobstructed (topmost element at their centre)
  const safetyGroup = page.getByTestId('console-safety-group')
  await expect(safetyGroup.getByRole('button', { name: /停止|已停止|停止中/ }).first()).toBeVisible()
  await expect(safetyGroup.getByRole('button', { name: /软件急停|解除急停/ }).first()).toBeVisible()
  await expect(page.locator('button.protective-stop')).toBeVisible()
  for (const [name, b] of [
    ['bar', bar],
    ['safety', safety],
    ['protective', protective],
    ['panel', panel],
  ] as const) {
    expect(b.x >= -1 && b.x + b.width <= viewport.width + 1, `${tag} ${name} inside width`).toBe(true)
    expect(b.y >= -1 && b.y + b.height <= viewport.height + 1, `${tag} ${name} inside height`).toBe(true)
  }
  const hit = await page.evaluate(
    (sels) =>
      sels.map((sel) => {
        const el = document.querySelector(sel) as HTMLElement | null
        if (!el) return `${sel}: missing`
        const r = el.getBoundingClientRect()
        const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)
        return top && (top === el || el.contains(top)) ? 'ok' : `${sel}: covered by ${top?.className}`
      }),
    [...UNOBSTRUCTED],
  )
  expect(hit, `${tag} safety buttons / panel tabs not covered`).toEqual(UNOBSTRUCTED.map(() => 'ok'))

  // 6. alarm popup (if any) never over the task bar, the safety group, or the control panel
  const popup = page.getByTestId('alarm-popup')
  if (await popup.count()) {
    const p = (await popup.boundingBox())!
    expect(disjoint(p, bar), `${tag} alarm popup over task bar`).toBe(true)
    expect(disjoint(p, safety), `${tag} alarm popup over safety group`).toBe(true)
    expect(disjoint(p, panel), `${tag} alarm popup over control panel`).toBe(true)
  }

  // 7. no region overlaps another, no horizontal scroll, KPI row stays one ordered row
  const regions = [panel, map, video, bar, status]
  for (let i = 0; i < regions.length; i++)
    for (let j = i + 1; j < regions.length; j++)
      expect(disjoint(regions[i], regions[j]), `${tag} regions ${i}/${j} overlap`).toBe(true)
  const scroll = await page.evaluate(() => ({
    doc: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }))
  expect(Math.max(scroll.doc, scroll.body), `${tag} no horizontal scroll`).toBeLessThanOrEqual(viewport.width)
  const kpis = await page.locator('.hmi-kpi').all()
  expect(kpis.length).toBe(4)
  for (let i = 1; i < kpis.length; i++) {
    const previous = (await kpis[i - 1].boundingBox())!
    const current = (await kpis[i].boundingBox())!
    expect(previous.x + previous.width).toBeLessThanOrEqual(current.x)
    expect(Math.abs(previous.y - current.y)).toBeLessThan(2)
  }
}

/** The left control panel exists ONLY on the console; other pages keep the original navigation. */
export async function assertPanelOnlyOnConsole(page: Page, otherRoutes: string[] = ['/tasks', '/alarms']) {
  for (const route of otherRoutes) {
    await page.goto(route)
    await expect(page.locator('.sidebar nav[aria-label="主导航"]')).toBeVisible()
    await expect(page.getByTestId('console-control-panel')).toHaveCount(0)
    await expect(page.locator('#sidebar-console-slot')).toBeHidden()
    await expect(page.locator('.sidebar')).not.toHaveClass(/sidebar--console/)
  }
}
