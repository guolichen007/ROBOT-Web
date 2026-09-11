import { expect, test, type APIRequestContext } from '@playwright/test'
import { getAccessToken as token, loginPage as login } from './helpers/auth'
import { collectRuntimeErrors } from './helpers/runtime-errors'
import { assertControlDockVisible } from './helpers/control-dock'
import {
  cleanupRobot,
  ensureRobotIdle,
  forceReleaseManualLease,
  getActiveTasksForRobot,
} from './helpers/robot-state'

// 服务器 readiness 合同断言：在点击按钮前分层暴露「服务器 readiness 红」vs「Web 投影/UI 问题」，
// 不再把两层混成一个 button timeout。失败信息只打印 readiness_reasons，绝不打印 token/password。
async function assertPatrolReady(request: APIRequestContext): Promise<void> {
  const accessToken = await token(request)
  const response = await request.get('/api/v1/monitor/snapshot', {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  expect(response.ok()).toBeTruthy()
  const snapshot = await response.json()
  const robot = snapshot.robots.find((item: { vehicle_id: string }) => item.vehicle_id === 'R001')
  const reasons = (robot?.readiness_reasons ?? []).join(', ')
  expect(robot, 'R001 not found in /monitor/snapshot').toBeTruthy()
  expect(robot.enabled, `R001.enabled expected true, readiness_reasons=[${reasons}]`).toBe(true)
  expect(robot.online_state, `R001.online_state expected ONLINE, readiness_reasons=[${reasons}]`).toBe(
    'ONLINE',
  )
  expect(robot.autonomous_task_ready?.patrol, `patrol not ready, readiness_reasons=[${reasons}]`).toBe(true)
  expect(
    robot.safety_command_ready?.stop_motion,
    `stop_motion not ready, readiness_reasons=[${reasons}]`,
  ).toBe(true)
  expect(robot.readiness_reasons ?? [], `readiness_reasons=[${reasons}]`).toEqual([])
}

test('industrial operations home shows map, roof camera and current control dock', async ({
  page,
  request,
}) => {
  const getRuntimeErrors = collectRuntimeErrors(page)
  await login(page, request)

  // 等待真实 R001 projection 后再断言，避免 snapshot reactive update 前提前 PASS
  await expect(page.locator('.device-snapshot')).toContainText('R001')
  await expect(page.getByText('车顶实时相机').first()).toBeVisible()
  // 当前 MOCK 的 estop/safety truth 会让 operationalSituation 正确进入 DEGRADED，
  // 验证正式 banner 语义（不再是旧 .situation-banner count=0）。
  await expect(page.getByText('系统降级，数据需核实')).toBeVisible()
  await expect(page.getByText('数据实时').first()).toBeVisible()
  // 控制 dock 用 state-dependent 动态标签合同，不锁死瞬时标签
  await assertControlDockVisible(page)
  await expect(page.getByRole('button', { name: '手动控制' })).toHaveCount(0)
  await expect(page.getByText('烟雾浓度')).toBeVisible()
  expect(getRuntimeErrors()).toEqual([])
})

test('media ticket is absent from URL and WHEP uses Authorization bearer', async ({ page, request }) => {
  await login(page, request)
  const accessToken = await token(request)
  const ticketResponse = await request.post('/api/v1/media/tickets', {
    headers: { Authorization: `Bearer ${accessToken}` },
    data: { stream_id: 'R001-roof_rgb' },
  })
  expect(ticketResponse.ok()).toBeTruthy()
  const issued = await ticketResponse.json()
  expect(issued.playback_url).not.toContain('token=')
  const anonymous = await request.post(issued.playback_url, {
    headers: { 'content-type': 'application/sdp' },
    data: 'invalid-sdp',
  })
  expect(anonymous.status()).toBe(401)
})

test('manual leases remain mutually exclusive via API', async ({ browser, request }) => {
  // Manual-control UI 收纳在抽屉；底层 lease 合同由 API 级互斥保护。
  await forceReleaseManualLease(request, 'R001')
  const first = await browser.newContext(),
    second = await browser.newContext()
  const firstToken = await token(first.request),
    secondToken = await token(second.request)
  const firstResponse = await first.request.post('/api/v1/robots/R001/manual-lease', {
    headers: { Authorization: `Bearer ${firstToken}` },
    data: { control_session_id: crypto.randomUUID() },
  })
  expect(firstResponse.status()).toBe(201)
  const secondResponse = await second.request.post('/api/v1/robots/R001/manual-lease', {
    headers: { Authorization: `Bearer ${secondToken}` },
    data: { control_session_id: crypto.randomUUID() },
  })
  expect(secondResponse.status()).toBe(409)
  const releaseResponse = await first.request.delete('/api/v1/robots/R001/manual-lease', {
    headers: { Authorization: `Bearer ${firstToken}` },
  })
  expect(releaseResponse.ok()).toBeTruthy()
  await first.close()
  await second.close()
})

test('stop patrol waits for task cancellation, stop ACK and five fresh stationary frames', async ({
  page,
  request,
}) => {
  await forceReleaseManualLease(request, 'R001')
  await ensureRobotIdle(request, 'R001')
  await assertPatrolReady(request)
  const getRuntimeErrors = collectRuntimeErrors(page)
  await login(page, request)

  // renderer 先稳定投影 R001（defer 后 host 就绪 + DeviceSnapshot R001 + 待命），
  // 再检查 runtime errors 与按钮，避免把 renderer 崩溃伪装成 button disabled。
  await expect(page.locator('#workspace-alert .monitor-situation-host')).toHaveCount(1)
  await expect(page.locator('.device-snapshot')).toContainText('R001')
  await expect(page.getByText('待命').first()).toBeVisible()
  expect(getRuntimeErrors()).toEqual([])

  try {
    const startButton = page.getByRole('button', { name: '开始巡检' })
    await expect(startButton).toBeEnabled({ timeout: 10_000 })
    await startButton.click()

    // 巡检启动三重验证：toast + UI 巡检执行中 + API 存在 active PATROL task（按 robot internal UUID）
    await expect(page.getByText(/巡检任务已创建/)).toBeVisible()
    await expect(page.getByText('巡检执行中').first()).toBeVisible()
    await expect
      .poll(
        async () => {
          const active = await getActiveTasksForRobot(request, 'R001')
          return active.some((task) => task.type === 'PATROL')
        },
        { timeout: 10_000 },
      )
      .toBe(true)

    // 点击停止前监听 stop-operation POST（权威合同：202 + operation.id）
    const stopResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname.endsWith('/api/v1/robots/R001/stop-operation'),
    )
    await page.getByRole('button', { name: '停止', exact: true }).click()

    const stopResponse = await stopResponsePromise
    expect(stopResponse.status()).toBe(202)
    const operation = await stopResponse.json()
    expect(operation.id).toBeTruthy()

    // 停止过程 strict-mode 断言（只匹配 stop-operation-state strong，不匹配 toast）
    await expect(
      page.locator('.stop-operation-state strong').getByText('正在停止车辆任务', { exact: true }),
    ).toBeVisible()

    // API authoritative：poll stop operation 到 terminal（不依赖瞬态 UI 5/5）
    const accessToken = await token(request)
    const headers = { Authorization: `Bearer ${accessToken}` }
    await expect
      .poll(
        async () => {
          const resp = await request.get(`/api/v1/stop-operations/${operation.id}`, { headers })
          expect(resp.ok()).toBeTruthy()
          return (await resp.json()).state
        },
        { timeout: 20_000 },
      )
      .toBe('VEHICLE_STATIONARY_CONFIRMED')

    const finalResp = await request.get(`/api/v1/stop-operations/${operation.id}`, { headers })
    const finalOp = await finalResp.json()
    expect(finalOp.stationary_frames).toBeGreaterThanOrEqual(5)

    // UI terminal 断言
    await expect(
      page.locator('.stop-operation-state strong').getByText('车辆已停止', { exact: true }),
    ).toBeVisible()
    await expect(
      page.locator('.stop-operation-state span').getByText('任务已取消，车辆静止已确认', { exact: true }),
    ).toBeVisible()

    // 最终确认：R001 active task 归零
    await expect
      .poll(async () => (await getActiveTasksForRobot(request, 'R001')).length, { timeout: 15_000 })
      .toBe(0)
    expect(getRuntimeErrors()).toEqual([])
  } finally {
    await cleanupRobot(request, 'R001')
  }
})

test('patrol report PDF and Excel use authenticated browser downloads', async ({ page, request }) => {
  await forceReleaseManualLease(request, 'R001')
  await ensureRobotIdle(request, 'R001')
  await login(page, request)
  const accessToken = await token(request)
  try {
    let tasksResponse = await request.get('/api/v1/tasks?limit=1000', {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
    let tasks = (await tasksResponse.json()) as Array<{ id: string; type: string; status: string }>
    let task = tasks.find((item) => item.type === 'PATROL' && item.status === 'SUCCEEDED')
    if (!task) {
      await page.getByRole('button', { name: '开始巡检' }).click()
      await ensureRobotIdle(request, 'R001')
      tasksResponse = await request.get('/api/v1/tasks?limit=1000', {
        headers: { Authorization: `Bearer ${accessToken}` },
      })
      tasks = await tasksResponse.json()
      task = tasks.find((item) => item.type === 'PATROL' && item.status === 'SUCCEEDED')
    }
    expect(task).toBeTruthy()
    const generated = await request.post(`/api/v1/patrol-reports/tasks/${task!.id}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
    expect(generated.ok()).toBeTruthy()
    const report = (await generated.json()) as { report_code: string }
    await page.goto('/patrol')
    const row = page.locator('.business-list-row').filter({ hasText: report.report_code })
    await expect(row).toBeVisible()
    const pdfDownload = page.waitForEvent('download')
    await row.getByRole('button', { name: 'PDF' }).click()
    expect((await pdfDownload).suggestedFilename()).toBe(`${report.report_code}.pdf`)
    const excelDownload = page.waitForEvent('download')
    await row.getByRole('button', { name: 'Excel' }).click()
    expect((await excelDownload).suggestedFilename()).toBe(`${report.report_code}.xlsx`)
  } finally {
    await cleanupRobot(request, 'R001')
  }
})
