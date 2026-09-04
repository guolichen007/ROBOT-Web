import { expect, test, type APIRequestContext } from '@playwright/test'
import { getAccessToken as token, loginPage as login } from './helpers/auth'

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

test('industrial operations home shows map, roof camera and current control dock', async ({
  page,
  request,
}) => {
  await login(page, request)
  await expect(page.locator('.situation-banner')).toHaveCount(0)
  await expect(page.getByText('车顶实时相机').first()).toBeVisible()
  // 当前冻结控制合同：开始巡检 / 停止 / 返回等待区；软件急停平台仍展示（真实 estop 未实现，仅可见性）。
  for (const name of ['开始巡检', '停止', '返回等待区', '软件急停']) {
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

test('manual leases remain mutually exclusive via API', async ({ browser, request }) => {
  // Manual-control UI 收纳在抽屉；底层 lease 合同由 API 级互斥保护。
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
  await page.getByRole('button', { name: '停止' }).click()
  await expect(page.getByText(/正在停止车辆任务/)).toBeVisible()
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
