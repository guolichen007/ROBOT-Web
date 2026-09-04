import { expect, test, type APIRequestContext } from '@playwright/test'
import { getAccessToken as token, loginPage } from './helpers/auth'

// 当前监控布局（current monitor layout）：只校验当前稳定合同，不复刻历史 ui-gate2 几何常量。
// 真实 emergency_stop / reset_estop 未实现，软件急停这里只校验可见性，不做锁存/复位流程。

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
    headers: { Authorization: `Bearer ${accessToken}`, 'Idempotency-Key': crypto.randomUUID() },
    data: {
      parking_slot_id: slot.id,
      fire_type: 'unknown',
      note: 'current-monitor-layout probe',
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

test('current monitor layout is stable across common resolutions', async ({ page, request }) => {
  await loginPage(page, request)
  for (const viewport of [
    { width: 2048, height: 997 },
    { width: 1920, height: 1080 },
    { width: 1366, height: 768 },
  ]) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height })

    // 当前稳定合同：主地图 + 车顶实时相机 + 控制区 + 三个运动控制 + 软件急停可见性
    await expect(page.getByRole('heading', { name: '停车场巡检地图' })).toBeVisible()
    await expect(page.getByText('车顶实时相机').first()).toBeVisible()
    await expect(page.locator('.operations-command-dock')).toBeVisible()
    await expect(page.getByRole('button', { name: '开始巡检' })).toBeVisible()
    await expect(page.getByRole('button', { name: '停止' })).toBeVisible()
    await expect(page.getByRole('button', { name: '返回等待区' })).toBeVisible()
    // 软件急停平台仍展示；真实 emergency_stop/reset_estop 未实现，这里只校验可见性
    await expect(page.getByRole('button', { name: '软件急停' })).toBeVisible()

    // 关键区域不溢出、无横向滚动、控制区不被裁切
    const meta = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    }))
    expect(meta.scrollWidth).toBeLessThanOrEqual(meta.innerWidth + 2)
    const dock = await page.locator('.operations-command-dock').boundingBox()
    expect(dock).not.toBeNull()
    expect(dock!.y + dock!.height).toBeLessThanOrEqual(viewport.height + 1)
  }
})

test('alarm smoke keeps monitor usable', async ({ page, request }) => {
  const alarmId = await createFire(request)
  try {
    await page.setViewportSize({ width: 1920, height: 1080 })
    await loginPage(page, request)
    await expect(page.locator('.situation-banner.critical')).toBeVisible()
    await expect(page.locator('.operations-command-dock')).toBeVisible()
    const meta = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    }))
    expect(meta.scrollWidth).toBeLessThanOrEqual(meta.innerWidth + 2)
    const dock = await page.locator('.operations-command-dock').boundingBox()
    expect(dock).not.toBeNull()
    expect(dock!.y + dock!.height).toBeLessThanOrEqual(1080 + 1)
  } finally {
    await resolveFire(request, alarmId)
  }
})
