import { expect, test, type APIRequestContext } from '@playwright/test'
import { getAccessToken as token, loginPage } from './helpers/auth'

async function waitForTaskTerminal(request: APIRequestContext, predicate: (task: any) => boolean) {
  const accessToken = await token(request)
  await expect
    .poll(
      async () => {
        const response = await request.get('/api/v1/tasks?limit=30', {
          headers: { Authorization: `Bearer ${accessToken}` },
        })
        const tasks = (await response.json()) as Array<{ status: string; type: string }>
        return tasks.some(predicate)
      },
      { timeout: 120_000, intervals: [1000, 2000] },
    )
    .toBe(true)
}

test('A-12 navigation preset moves robot to the slot inspection pose', async ({ page, request }) => {
  const accessToken = await token(request)
  // Ensure the robot is idle and not latched before starting.
  const state = await request.get('/api/v1/robots/R001', {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if ((await state.json()).estop_active) {
    await request.post('/api/v1/robots/R001/commands/reset-estop', {
      headers: { Authorization: `Bearer ${accessToken}`, 'Idempotency-Key': crypto.randomUUID() },
    })
  }

  await loginPage(page, request)
  await page.getByRole('button', { name: '车位 A-12' }).click()
  const navigate = page.getByRole('button', { name: '确认前往检测点' })
  await expect(navigate).toBeEnabled()
  await navigate.click()
  await expect(page.getByText(/已创建前往 A-12 检测点的任务/)).toBeVisible()

  await waitForTaskTerminal(request, (task) => task.status === 'SUCCEEDED')

  const coverage = await request.get('/api/v1/robots/R001/detection-coverage', {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  expect(coverage.ok()).toBeTruthy()
  const body = await coverage.json()
  const snapshot = await request.get('/api/v1/monitor/snapshot', {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  const snap = await snapshot.json()
  const slot = snap.parking_slots.find((item: { code: string }) => item.code === 'A-12')
  expect(slot).toBeTruthy()
  expect(body.covered_parking_slot_ids).toContain(slot.id)
})

test('right-side S-cruise patrol reaches completion', async ({ page, request }) => {
  const accessToken = await token(request)
  const state = await request.get('/api/v1/robots/R001', {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  if ((await state.json()).estop_active) {
    await request.post('/api/v1/robots/R001/commands/reset-estop', {
      headers: { Authorization: `Bearer ${accessToken}`, 'Idempotency-Key': crypto.randomUUID() },
    })
  }

  await loginPage(page, request)
  await page.getByRole('button', { name: '开始巡检' }).click()
  await expect(page.getByText(/巡检任务已创建/)).toBeVisible()
  await expect(page.getByText(/正在确认车辆静止/)).toBeHidden()

  await waitForTaskTerminal(request, (task) => task.status === 'SUCCEEDED' && task.type === 'PATROL')
})
