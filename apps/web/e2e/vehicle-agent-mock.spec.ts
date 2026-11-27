import { expect, test } from '@playwright/test'
import { CONSOLE_VIEWPORTS, assertConsoleLayout, assertPanelOnlyOnConsole } from './helpers/console-layout'

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
  // G3-R2：无独立温度传感器 → 首页固定四项指标（mock 仍上报兼容 thermal 字段，也不得显示温度）
  await expect(page.locator('.hmi-kpi[data-stale="false"]')).toHaveCount(4)
  await expect(page.locator('[data-kpi="temperature"]')).toHaveCount(0)
  await expect(page.locator('.hmi-kpis')).not.toContainText('温度')
  await expect(page.locator('.hmi-kpis')).not.toContainText('℃')
  await expect(page.locator('.hmi-camera')).toHaveCount(2)
  await expect(page.getByRole('heading', { name: '监控相机', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '红外热成像', exact: true })).toBeVisible()
  await expect(page.locator('.video-tabs')).toHaveCount(0)

  // WEB-R1C OWNER-approved layout (left control panel below 系统设置, stacked videos on the right,
  // fixed bottom task bar with an always-visible safety group) at 3 viewports. The vehicle_agent
  // task buttons render in the bottom bar and start/close in the left 设备 tab, from ONE shared
  // command tracker (no second copy of command state).
  for (const viewport of CONSOLE_VIEWPORTS) {
    await page.setViewportSize(viewport)
    await assertConsoleLayout(page, viewport)
    await page.screenshot({
      path: `artifacts/vehicle-agent-hmi-${viewport.width}x${viewport.height}.png`,
      fullPage: true,
    })
  }
  await page.setViewportSize({ width: 1920, height: 1080 })

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
  // ACK accepts the goal; no arrival is fabricated while the mock remains spot.
  await expect(page.getByTestId('agent-task-state')).toHaveText('NAVIGATING / 前往目标')
  await act('stop_navigate')
  await expect(page.getByTestId('agent-task-state')).toHaveText('IDLE / 空闲')

  const scenarioUrl = process.env.MOCK_SCENARIO_URL || 'http://127.0.0.1:18765/__mock__/scenario'
  await request.post(scenarioUrl, { data: { action: 'failure', code: 'NOT_READY' } })
  await act('start')
  await expect(page.locator('.hmi-command-error')).toHaveAttribute('data-code', 'NOT_READY')
  await request.post(scenarioUrl, { data: { action: 'failure', code: 'BUSY' } })
  await act('start')
  await expect(page.locator('.hmi-command-error')).toHaveText('mock start: BUSY')
  await expect(page.locator('.hmi-command-error')).toHaveAttribute('data-code', 'BUSY')
  await request.post(scenarioUrl, { data: { action: 'failure', code: null } })
  await request.post(scenarioUrl, { data: { action: 'offline' } })
  await expect(page.locator('.hmi-kpi[data-stale="true"]')).toHaveCount(4)
  await expect(page.locator('[data-action="patrol"]')).toBeDisabled()
  await request.post(scenarioUrl, { data: { action: 'online' } })
  await expect(page.locator('[data-action="homing"]')).toBeEnabled()
  await act('homing')
  await expect(page.getByTestId('agent-task-state')).toHaveText('HOMING / 返航中')
  if (process.env.VEHICLE_AGENT_CONTAINER_E2E === '1') {
    const before = await (await request.post(scenarioUrl, { data: { action: 'duplicate' } })).json()
    await expect
      .poll(async () => {
        const after = await (await request.post(scenarioUrl, { data: { action: 'inspect' } })).json()
        expect(after.last_wire.msg_id).toBe(before.last_wire.msg_id)
        expect(after.executions.homing).toBe(before.executions.homing)
        return after.deliveries
      })
      .toBeGreaterThanOrEqual(before.delivery_target)
  }
  await request.post(scenarioUrl, { data: { action: 'arrive' } })
  await expect(page.getByTestId('agent-task-state')).toHaveText('IDLE / 空闲')
  await act('navigate')
  await expect(page.getByTestId('agent-task-state')).toHaveText('NAVIGATING / 前往目标')
  await request.post(scenarioUrl, { data: { action: 'arrive' } })
  await expect(page.getByTestId('agent-task-state')).toHaveText('IDLE / 空闲')
  await act('close')
  // the left panel exists only on the console; other pages keep their navigation untouched
  await assertPanelOnlyOnConsole(page)
  expect(errors).toEqual([])
})
