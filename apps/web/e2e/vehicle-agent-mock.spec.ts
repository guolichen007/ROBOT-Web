import { expect, test } from '@playwright/test'

test.skip(process.env.VEHICLE_AGENT_MOCK_E2E !== '1', 'requires isolated vehicle_agent mock runtime')

test('Browser → API → Outbox → adapter → mock → ACK/status → WS and industrial HMI', async ({
  page,
  request,
}) => {
  test.setTimeout(120_000)
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/login')
  await page.getByPlaceholder('请输入账号').fill('admin')
  await page.getByPlaceholder('请输入密码').fill(process.env.E2E_ADMIN_PASSWORD || 'MockOnly-ChangeMe-2026!')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '停车场二维地图' })).toBeVisible()
  await expect(page.locator('.hmi-link-state')).toContainText('已连接')
  await expect(page.locator('.hmi-camera')).toHaveCount(2)
  await expect(page.getByRole('heading', { name: '监控相机', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '红外热成像', exact: true })).toBeVisible()
  await expect(page.locator('.video-tabs')).toHaveCount(0)

  for (const viewport of [
    { width: 1920, height: 1080 },
    { width: 1680, height: 900 },
  ]) {
    await page.setViewportSize(viewport)
    const map = (await page.locator('.operations-map-panel').boundingBox())!
    const video = (await page.locator('.video-surveillance').boundingBox())!
    const control = (await page.locator('.hmi-control-rail').boundingBox())!
    expect(map.x + map.width).toBeLessThanOrEqual(video.x)
    expect(video.x + video.width).toBeLessThanOrEqual(control.x)
    expect(Math.abs(map.y - video.y)).toBeLessThan(2)
    expect(Math.abs(map.y - control.y)).toBeLessThan(2)
    expect(control.x + control.width).toBeLessThanOrEqual(viewport.width)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      viewport.width,
    )
    const kpis = await page.locator('.hmi-kpi').all()
    for (let i = 1; i < kpis.length; i++) {
      const previous = (await kpis[i - 1].boundingBox())!
      const current = (await kpis[i].boundingBox())!
      expect(previous.x + previous.width).toBeLessThanOrEqual(current.x)
    }
    await page.screenshot({
      path: `artifacts/vehicle-agent-hmi-${viewport.width}x${viewport.height}.png`,
      fullPage: true,
    })
  }

  const dropdown = page.getByRole('combobox', { name: '停车位', exact: true })
  await dropdown.selectOption({ index: 1 })
  await expect(page.locator('.parking-slot.selected')).toHaveCount(1)
  const secondSlot = page.locator('.slot-group').nth(1)
  const code = (await secondSlot.getAttribute('aria-label'))!.replace('车位 ', '')
  await secondSlot.click()
  await expect(dropdown.locator('option:checked')).toHaveText(code)
  await expect(page.locator('[data-action="resume_patrol"]')).toBeDisabled()
  await expect(page.locator('[data-action="navigate"]')).toBeDisabled()

  async function act(action: string) {
    const button = page.locator(`[data-action="${action}"]`)
    await expect(button).toBeEnabled()
    await button.click()
    await expect(button).toHaveAttribute('aria-busy', 'false')
  }
  await act('start')
  await act('patrol')
  await expect(page.getByTestId('agent-task-state')).toHaveText('PATROL / 巡逻中')
  await expect(page.locator('[data-action="navigate"]')).toBeDisabled()
  await act('stop_patrol')
  await expect(page.locator('[data-action="resume_patrol"]')).toBeEnabled()
  await act('resume_patrol')
  await act('stop_patrol')
  await act('close')
  await act('start')
  await act('navigate')
  await expect(page.getByTestId('agent-task-state')).toHaveText('NAVIGATING / 前往目标')
  await act('stop_navigate')
  await expect(page.getByTestId('agent-task-state')).toHaveText('IDLE / 空闲')

  const scenarioUrl = process.env.MOCK_SCENARIO_URL || 'http://127.0.0.1:18765/__mock__/scenario'
  await request.post(scenarioUrl, { data: { action: 'failure', code: 'BUSY' } })
  await act('start')
  await expect(page.locator('.hmi-command-error')).toHaveText('mock start: BUSY')
  await expect(page.locator('.hmi-command-error')).toHaveAttribute('data-code', 'BUSY')
  await request.post(scenarioUrl, { data: { action: 'failure', code: null } })
  await request.post(scenarioUrl, { data: { action: 'offline' } })
  await expect(page.locator('.hmi-kpi[data-stale="true"]')).toHaveCount(5)
  await expect(page.locator('[data-action="patrol"]')).toBeDisabled()
  await request.post(scenarioUrl, { data: { action: 'online' } })
  await expect(page.locator('[data-action="homing"]')).toBeEnabled()
  await act('homing')
  await expect(page.getByTestId('agent-task-state')).toHaveText('HOMING / 返航中')
  await request.post(scenarioUrl, { data: { action: 'arrive' } })
  await expect(page.getByTestId('agent-task-state')).toHaveText('IDLE / 空闲')
  await act('close')
  expect(errors).toEqual([])
})
