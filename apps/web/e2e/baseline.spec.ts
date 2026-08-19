import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const configuredPassword = process.env.E2E_ADMIN_PASSWORD
const password = configuredPassword || 'Firebot-Dev-2026!'
const changedPassword = process.env.E2E_CHANGED_PASSWORD || 'Firebot-E2E-Changed-2026!'

async function workingPassword(request: APIRequestContext): Promise<{ value: string; mustChange: boolean }> {
  // The first test rotates the bootstrap credential. Every later test in the
  // same isolated profile must therefore try the changed password first even
  // when CI explicitly supplied E2E_ADMIN_PASSWORD.
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

async function token(request: APIRequestContext): Promise<string> {
  const credentials = await workingPassword(request)
  const response = await request.post('/api/v1/auth/login', {
    data: { username: 'admin', password: credentials.value },
  })
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token
}

async function forceRelease(request: APIRequestContext): Promise<void> {
  const accessToken = await token(request)
  await request.post('/api/v1/robots/R001/manual-lease/force-release', {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
}

async function waitForRobotIdle(request: APIRequestContext): Promise<void> {
  const accessToken = await token(request)
  await expect
    .poll(
      async () => {
        const response = await request.get('/api/v1/tasks?limit=30', {
          headers: { Authorization: `Bearer ${accessToken}` },
        })
        expect(response.ok()).toBeTruthy()
        const tasks = (await response.json()) as Array<{ robot_id: string; status: string }>
        return tasks.some((task) => ['CREATED', 'QUEUED', 'ACCEPTED', 'EXECUTING'].includes(task.status))
      },
      { timeout: 20_000, intervals: [500, 1_000] },
    )
    .toBe(false)
}

test('industrial operations home prioritizes map, roof camera and four controls', async ({
  page,
  request,
}) => {
  await login(page, request)
  await expect(page.locator('.situation-banner')).toHaveCount(0)
  await expect(page.getByText('车顶实时相机').first()).toBeVisible()
  for (const name of ['开始巡检', '停止巡检', '返回等待区', '软件急停']) {
    await expect(page.getByRole('button', { name })).toBeVisible()
  }
  await expect(page.getByRole('button', { name: '手动控制' })).toHaveCount(0)
  await expect(page.getByText('烟雾浓度')).toBeVisible()
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

test('A-12 manual fire dispatches extinguish directly without confirm chain', async ({
  page,
  request,
}) => {
  await forceRelease(request)
  await login(page, request)
  await page.getByRole('button', { name: '车位 A-12' }).click()
  await page.getByRole('button', { name: '人工上报火情' }).click()
  await expect(page.getByText(/A-12 人工火情已创建/)).toBeVisible()
  await expect(page.getByText('展开灭火帐', { exact: true })).toBeVisible()
  await expect(page.getByText('喷射灭火剂', { exact: true })).toBeVisible()
  await expect(page.getByText('灭火帐 + 喷射', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '展开灭火帐' }).click()
  await expect(page.getByText(/灭火帐任务已下发/)).toBeVisible()
  await waitForRobotIdle(request)
})

test('manual leases remain mutually exclusive via API', async ({ browser, request }) => {
  // Manual-control UI is paused (Gate-3); the underlying lease contract stays
  // protected by API-level integration tests.
  await forceRelease(request)
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
  await first.request.delete('/api/v1/robots/R001/manual-lease', {
    headers: { Authorization: `Bearer ${firstToken}` },
  })
  await first.close()
  await second.close()
})

test('stop patrol waits for task cancellation, stop ACK and five fresh stationary frames', async ({
  page,
  request,
}) => {
  await forceRelease(request)
  await waitForRobotIdle(request)
  await login(page, request)
  await page.getByRole('button', { name: '开始巡检' }).click()
  await expect(page.getByText(/巡检任务已创建/)).toBeVisible()
  await expect
    .poll(async () => (await page.getByText(/PATROL|EXECUTING/).count()) > 0, { timeout: 10_000 })
    .toBe(true)
  await page.getByRole('button', { name: '停止巡检' }).click()
  await expect(page.getByText(/正在确认车辆静止/)).toBeVisible()
  await expect(page.getByText('车辆已停止')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText(/连续静止帧 5\/5/)).toBeVisible()
})

test('patrol report PDF and Excel use authenticated browser downloads', async ({ page, request }) => {
  await forceRelease(request)
  await waitForRobotIdle(request)
  await login(page, request)
  const accessToken = await token(request)
  let tasksResponse = await request.get('/api/v1/tasks?limit=100', {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  let tasks = (await tasksResponse.json()) as Array<{ id: string; type: string; status: string }>
  let task = tasks.find((item) => item.type === 'PATROL' && item.status === 'SUCCEEDED')
  if (!task) {
    await page.getByRole('button', { name: '开始巡检' }).click()
    await waitForRobotIdle(request)
    tasksResponse = await request.get('/api/v1/tasks?limit=100', {
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
})
