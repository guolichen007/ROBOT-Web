import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test'

// Multi-vehicle (R001 + R002) isolation and active-vehicle switch acceptance.
// Runs against compose.test.yml --profile full where mock-robot (R001) and
// mock-robot-2 (R002) are both online.
//
// 诚实边界（不写假测试）：
// - ALARM 隔离：当前 API 的手工火情 /alarms/manual 创建的是 site-level 火情
//   （FireEvent.robot_id = null），没有接口能确定性构造“属于 R001 的活动告警”，
//   因此本文件不伪造 ALARM_ISOLATION 测试。
// - R002 OFFLINE：需要停止 mock-robot-2 并等待 offline TTL，属于 Docker/运维动作，
//   Playwright 不能安全控制，故拆到 Windows host live acceptance，不在此伪造。

const configuredPassword = process.env.E2E_ADMIN_PASSWORD
const password = configuredPassword || 'Firebot-Dev-2026!'
const changedPassword = process.env.E2E_CHANGED_PASSWORD || 'Firebot-E2E-Changed-2026!'

async function workingPassword(request: APIRequestContext): Promise<{ value: string; mustChange: boolean }> {
  const candidates = [...new Set([changedPassword, configuredPassword, password].filter(Boolean))]
  for (const candidate of candidates) {
    const response = await request.post('/api/v1/auth/login', {
      data: { username: 'admin', password: candidate },
    })
    if (response.ok())
      return { value: candidate, mustChange: Boolean((await response.json()).user.must_change_password) }
  }
  throw new Error('No valid E2E admin password')
}

// 统一语义：API-only 测试（token/snapshot）必须处理 bootstrap 首次改密，
// 保证在全新 seed DB 上单独跑 multi-vehicle.spec.ts 也成立，不依赖 baseline 先改密。
async function ensurePasswordReady(request: APIRequestContext): Promise<string> {
  const credentials = await workingPassword(request)
  if (!credentials.mustChange) {
    const login = await request.post('/api/v1/auth/login', {
      data: { username: 'admin', password: credentials.value },
    })
    expect(login.ok()).toBeTruthy()
    return (await login.json()).access_token
  }
  const login = await request.post('/api/v1/auth/login', {
    data: { username: 'admin', password: credentials.value },
  })
  expect(login.ok()).toBeTruthy()
  const accessToken = (await login.json()).access_token
  const change = await request.post('/api/v1/auth/change-password', {
    headers: { Authorization: `Bearer ${accessToken}` },
    data: { current_password: credentials.value, new_password: changedPassword },
  })
  expect(change.ok()).toBeTruthy()
  const relogin = await request.post('/api/v1/auth/login', {
    data: { username: 'admin', password: changedPassword },
  })
  expect(relogin.ok()).toBeTruthy()
  return (await relogin.json()).access_token
}

async function login(page: Page, request: APIRequestContext): Promise<void> {
  const credentials = await workingPassword(request)
  await page.goto('/login')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(credentials.value)
  await page.getByRole('button', { name: '登录' }).click()
  if (credentials.mustChange) {
    await page.getByLabel('当前密码').fill(credentials.value)
    await page.getByLabel('新密码', { exact: true }).fill(changedPassword)
    await page.getByLabel('确认新密码').fill(changedPassword)
    await page.getByRole('button', { name: '修改并重新登录' }).click()
    await expect(page).toHaveURL(/\/login$/)
    await page.getByLabel('密码', { exact: true }).fill(changedPassword)
    await page.getByRole('button', { name: '登录' }).click()
  }
  await expect(page.getByRole('heading', { name: '停车场巡检地图' })).toBeVisible()
}

async function snapshot(request: APIRequestContext): Promise<{
  robots: Array<{ id?: string; vehicle_id: string }>
  streams: Array<{ stream_id: string; robot_id: string }>
  tasks: Array<{ robot_id: string }>
  trajectories: Array<{ id: string }>
  parking_slots: Array<{ id: string }>
}> {
  const accessToken = await ensurePasswordReady(request)
  const response = await request.get('/api/v1/monitor/snapshot', {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  expect(response.ok()).toBeTruthy()
  return await response.json()
}

function deviceRow(page: Page, label: string): Locator {
  return page
    .locator('.device-snapshot .ds-grid > div')
    .filter({ has: page.getByText(label, { exact: true }) })
    .locator('dd')
}

async function switchToR002(page: Page, request: APIRequestContext): Promise<void> {
  await login(page, request)
  await page.goto('/robots')
  await page.locator('.vehicle-row').filter({ hasText: 'R002' }).getByRole('button', { name: '切换监控' }).click()
  await expect(page.getByText('已切换当前监控车辆：R002')).toBeVisible()
}

// 1) API-only 测试独立：在全新 seed 上单独可跑；快照含 R001+R002，且 media 按 robot 隔离。
test('snapshot lists R001 and R002 with robot-scoped streams', async ({ request }) => {
  const snap = await snapshot(request)
  const ids = snap.robots.map((r) => r.vehicle_id)
  expect(ids).toContain('R001')
  expect(ids).toContain('R002')
  for (const stream of snap.streams) {
    expect(stream.stream_id.startsWith(`${stream.robot_id}-`)).toBeTruthy()
  }
})

// 2) 真正进入 Monitor，断言 DeviceSnapshot 的“机器人编号”= R002（不是 R001）。
test('monitor device snapshot shows R002 as the active vehicle', async ({ page, request }) => {
  await switchToR002(page, request)
  await page.goto('/monitor')
  await expect(page.getByRole('heading', { name: '停车场巡检地图' })).toBeVisible()
  await expect(page.locator('.device-snapshot')).toBeVisible()
  await expect(deviceRow(page, '机器人编号')).toHaveText('R002')
})

// 3) TASK 隔离：确定性创建 R001 任务，切到 R002 后当前任务必须仍为“空闲/--”。
test('R001 active task does not leak into the R002 monitor view', async ({ page, request }) => {
  const accessToken = await ensurePasswordReady(request)
  const base = await snapshot(request)
  const slot = base.parking_slots[0]
  const created = await request.post('/api/v1/tasks/patrol', {
    headers: { Authorization: `Bearer ${accessToken}`, 'Idempotency-Key': crypto.randomUUID() },
    data: {
      robot_id: 'R001',
      target_parking_slot_id: slot.id,
      trajectory_id: base.trajectories[0]?.id,
      parameters: {},
    },
  })
  expect(created.ok()).toBeTruthy()

  // 反证：R001 此刻确有活动任务。
  const after = await snapshot(request)
  const r001 = after.robots.find((r) => r.vehicle_id === 'R001')
  expect(after.tasks.some((t) => t.robot_id === r001?.id)).toBeTruthy()

  await switchToR002(page, request)
  await page.goto('/monitor')
  await expect(page.locator('.device-snapshot')).toBeVisible()
  await expect(deviceRow(page, '当前任务')).toHaveText('空闲')
  await expect(deviceRow(page, '任务编号')).toHaveText('--')
})

// 4) MEDIA 隔离：R001 的 roof_rgb 是 LIVE（media-test-source），R002 视图不得出现 live tag，
//    只能显示 R002 自己的 OFFLINE 占位（“视频未连接”）。
test('R001 live media does not leak into the R002 video panel', async ({ page, request }) => {
  await switchToR002(page, request)
  await page.goto('/monitor')
  await expect(page.locator('.video-surveillance')).toBeVisible()
  await expect(page.locator('.video-live-tag')).toHaveCount(0)
  await expect(page.locator('.video-placeholder strong')).toContainText('视频未连接')
})

// 5) 真实 WS 断线重连（setOffline，非 reload）：断网 → 链路状态“正在重连”；
//    恢复 → “正常”，且 active vehicle 仍为 R002。
//    依赖真实浏览器网络模拟，需在 live 环境执行确认。
test('websocket reconnect preserves the R002 active selection', async ({ page, request }) => {
  await switchToR002(page, request)
  const linkCell = page.locator('.status-cell').filter({ hasText: '链路状态' })
  await expect(linkCell).toContainText('正常')

  await page.context().setOffline(true)
  await expect(linkCell).toContainText('正在重连', { timeout: 15_000 })

  await page.context().setOffline(false)
  await expect(linkCell).toContainText('正常', { timeout: 20_000 })
  expect(await page.evaluate(() => localStorage.getItem('firebot.activeVehicleId'))).toBe('R002')
})

// 6) 刷新持久化：整页 reload 后 active vehicle 仍为 R002。
test('active R002 selection persists across reload', async ({ page, request }) => {
  await switchToR002(page, request)
  await page.reload()
  await expect(page.getByRole('heading', { name: '车辆配置' })).toBeVisible()
  await expect.poll(() => page.evaluate(() => localStorage.getItem('firebot.activeVehicleId'))).toBe('R002')
})
