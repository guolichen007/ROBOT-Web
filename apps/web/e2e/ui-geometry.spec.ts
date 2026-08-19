import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { mkdirSync } from 'node:fs'

const password = process.env.E2E_ADMIN_PASSWORD || 'Firebot-Dev-2026!'
const SHOT_DIR = 'screenshots/ui-gate2'

// 100% browser zoom is enforced by Playwright defaults (deviceScaleFactor = 1,
// no zoom). We still record it per capture so the report can prove it.
async function captureMeta(page: Page) {
  return page.evaluate(() => ({
    viewport: { width: window.innerWidth, height: window.innerHeight },
    devicePixelRatio: window.devicePixelRatio,
    scrollWidth: document.documentElement.scrollWidth,
    scrollHeight: document.documentElement.scrollHeight,
  }))
}

async function token(request: APIRequestContext): Promise<string> {
  const response = await request.post('/api/v1/auth/login', {
    data: { username: 'admin', password },
  })
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token
}

async function login(page: Page): Promise<void> {
  await page.goto('/login')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page.getByRole('heading', { name: '停车场巡检地图' })).toBeVisible()
}

async function createFire(request: APIRequestContext): Promise<string> {
  const accessToken = await token(request)
  const snapshot = await request.get('/api/v1/monitor/snapshot', {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  expect(snapshot.ok()).toBeTruthy()
  const body = await snapshot.json()
  const slot = body.parking_slots.find((item: { enabled: boolean }) => item.enabled)
  expect(slot).toBeTruthy()
  const created = await request.post('/api/v1/alarms/manual', {
    headers: { Authorization: `Bearer ${accessToken}` },
    data: {
      parking_slot_id: slot.id,
      fire_type: 'unknown',
      note: 'ui-geometry probe',
      map_version: body.map_version?.version || '1',
      severity: 'HIGH',
      media: {},
    },
  })
  expect(created.ok()).toBeTruthy()
  return (await created.json()).id
}

async function resolveFire(request: APIRequestContext, id: string): Promise<void> {
  const accessToken = await token(request)
  await request.post(`/api/v1/alarms/${id}/resolve`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
}

test.describe('ui-gate2 viewport geometry', () => {
  test.beforeAll(() => {
    mkdirSync(SHOT_DIR, { recursive: true })
  })

  for (const viewport of [
    { name: '2048x997', width: 2048, height: 997 },
    { name: '1920x1080', width: 1920, height: 1080 },
    { name: '1672x941', width: 1672, height: 941 },
    { name: '1440x900', width: 1440, height: 900 },
    { name: '1366x768', width: 1366, height: 768 },
  ]) {
    test(`normal ${viewport.name}`, async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height })
      await login(page)

      const meta = await captureMeta(page)
      expect(meta.devicePixelRatio).toBe(1)

      // no situation banner in normal state
      await expect(page.locator('.situation-banner')).toHaveCount(0)

      const sidebar = await page.locator('.sidebar').boundingBox()
      const topbar = await page.locator('.topbar').boundingBox()
      const dock = await page.locator('.yd-command-dock').boundingBox()
      expect(sidebar).not.toBeNull()
      expect(sidebar!.width).toBeGreaterThanOrEqual(185)
      expect(sidebar!.width).toBeLessThanOrEqual(230)
      expect(topbar).not.toBeNull()
      expect(topbar!.height).toBeGreaterThanOrEqual(64)
      expect(topbar!.height).toBeLessThanOrEqual(92)
      expect(dock).not.toBeNull()
      expect(dock!.y + dock!.height).toBeLessThanOrEqual(viewport.height + 1)

      // telemetry is consolidated in the topbar status strip
      await expect(page.locator('.status-telemetry')).toHaveCount(4)
      await expect(page.locator('.device-snapshot')).toBeVisible()

      // 4 command buttons, no clipped control
      for (const name of ['开始巡检', '停止巡检', '返回等待区', '软件急停']) {
        await expect(page.getByRole('button', { name })).toBeVisible()
      }
      await expect(page.getByRole('button', { name: '手动控制' })).toHaveCount(0)

      // no page-level horizontal scroll
      expect(meta.scrollWidth).toBeLessThanOrEqual(viewport.width + 2)

      await page.screenshot({ path: `${SHOT_DIR}/${viewport.name}-normal.png` })
    })
  }

  for (const viewport of [
    { name: '1672x941', width: 1672, height: 941 },
    { name: '1366x768', width: 1366, height: 768 },
  ]) {
    test(`alarm ${viewport.name}`, async ({ page, request }) => {
      const alarmId = await createFire(request)
      try {
        await page.setViewportSize({ width: viewport.width, height: viewport.height })
        await login(page)

        const meta = await captureMeta(page)
        expect(meta.devicePixelRatio).toBe(1)

        // fire banner above topbar
        const banner = page.locator('.situation-banner.critical')
        await expect(banner).toBeVisible()
        const bannerBox = await banner.boundingBox()
        const topbarBox = await page.locator('.topbar').boundingBox()
        expect(bannerBox).not.toBeNull()
        expect(topbarBox).not.toBeNull()
        expect(bannerBox!.y).toBeLessThan(topbarBox!.y)

        // primary alarm detail/actions side by side
        const detail = await page.locator('.alarm-detail-column').boundingBox()
        const actions = await page.locator('.alarm-actions-column').boundingBox()
        expect(detail).not.toBeNull()
        expect(actions).not.toBeNull()
        expect(Math.abs(detail!.y - actions!.y)).toBeLessThanOrEqual(4)
        expect(detail!.width).toBeGreaterThan(actions!.width)

        // other events collapsed
        const other = await page.locator('.secondary-alarms').boundingBox()
        expect(other).not.toBeNull()
        expect(other!.height).toBeLessThanOrEqual(72)

        // dock still fully visible, no clipped control
        const dock = await page.locator('.yd-command-dock').boundingBox()
        expect(dock).not.toBeNull()
        expect(dock!.y + dock!.height).toBeLessThanOrEqual(viewport.height + 1)

        await page.screenshot({ path: `${SHOT_DIR}/${viewport.name}-alarm.png` })
      } finally {
        await resolveFire(request, alarmId)
      }
    })
  }
})
