import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const password = process.env.E2E_ADMIN_PASSWORD || 'Firebot-Dev-2026!'

async function login(page: Page): Promise<void> {
  await page.goto('/login')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '进入平台' }).click()
  await expect(page.getByRole('heading', { name: '态势监控' })).toBeVisible()
}

async function token(request: APIRequestContext): Promise<string> {
  const response = await request.post('/api/v1/auth/login', {
    data: { username: 'admin', password },
  })
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token
}

test('login and R001 live monitor baseline', async ({ page }) => {
  await login(page)
  await expect(page.getByText('R001', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('parking_v1', { exact: false })).toBeVisible()
  await expect(page.getByText('视频源未连接').first()).toBeVisible()
  await expect(page.getByText('软件急停不等于物理急停')).toBeVisible()
})

test('map A-12 creates a manual fire event', async ({ page }) => {
  await login(page)
  await page.getByRole('button', { name: '车位 A-12' }).click()
  await page.getByRole('button', { name: '创建人工火情' }).click()
  await expect(page.getByText(/A-12 人工火情已创建/)).toBeVisible()
})

test('two clients cannot hold the same manual lease', async ({ browser }) => {
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

test('manual pointer release stops pulses and releases the lease', async ({ page }) => {
  await login(page)
  const forward = page.getByRole('button', { name: '↑ 前进' })
  await forward.dispatchEvent('pointerdown')
  await expect(page.getByText('租约 HELD')).toBeVisible()
  await page.waitForTimeout(650)
  await page.dispatchEvent('body', 'pointerup')
  await expect(page.getByText('无租约')).toBeVisible()
  await expect(page.getByText(/停止指令已发送/)).toBeVisible()
})

test('quick click cannot start pulses after pointer release', async ({ page }) => {
  await login(page)
  await page.getByRole('button', { name: '↑ 前进' }).click()
  await page.waitForTimeout(800)
  await expect(page.getByText('无租约')).toBeVisible()
})
