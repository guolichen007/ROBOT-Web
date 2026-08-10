import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const password = process.env.E2E_ADMIN_PASSWORD || 'Firebot-Dev-2026!'
const changedPassword = process.env.E2E_CHANGED_PASSWORD || 'Firebot-E2E-Changed-2026!'

async function workingPassword(request: APIRequestContext): Promise<{ value: string; mustChange: boolean }> {
  for (const candidate of [changedPassword, password]) {
    const response = await request.post('/api/v1/auth/login', {
      data: { username: 'admin', password: candidate },
    })
    if (response.ok()) {
      return { value: candidate, mustChange: Boolean((await response.json()).user.must_change_password) }
    }
  }
  throw new Error('Neither the bootstrap nor E2E-rotated admin password is valid')
}

async function login(page: Page, request: APIRequestContext): Promise<void> {
  const credentials = await workingPassword(request)
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await page.goto('/login')
      break
    } catch (error) {
      if (attempt === 1 || !String(error).includes('ERR_NETWORK_CHANGED')) throw error
      await page.waitForTimeout(500)
    }
  }
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(credentials.value)
  await page.getByRole('button', { name: '进入平台' }).click()
  if (credentials.mustChange) {
    await expect(page.getByRole('heading', { name: '修改初始密码' })).toBeVisible()
    await page.getByLabel('当前密码').fill(credentials.value)
    await page.getByLabel('新密码', { exact: true }).fill(changedPassword)
    await page.getByLabel('确认新密码').fill(changedPassword)
    await page.getByRole('button', { name: '修改并重新登录' }).click()
    await expect(page.getByRole('heading', { name: '登录控制台' })).toBeVisible()
    await page.getByLabel('密码').fill(changedPassword)
    await page.getByRole('button', { name: '进入平台' }).click()
  }
  await expect(page.getByRole('heading', { name: '态势监控' })).toBeVisible()
}

async function token(request: APIRequestContext): Promise<string> {
  const credentials = await workingPassword(request)
  const response = await request.post('/api/v1/auth/login', {
    data: { username: 'admin', password: credentials.value },
  })
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token
}

test('login and R001 live monitor baseline', async ({ page, request }) => {
  await login(page, request)
  await expect(page.getByText('R001', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('parking_v1', { exact: false })).toBeVisible()
  await expect(page.getByText('视频源未连接').first()).toBeVisible()
  await expect(page.getByText('软件急停不等于物理急停')).toBeVisible()
})

test('map A-12 creates, confirms and dispatches a manual fire event', async ({ page, request }) => {
  await login(page, request)
  await page.getByRole('button', { name: '车位 A-12' }).click()
  await page.getByRole('button', { name: '创建人工火情' }).click()
  await expect(page.getByText(/A-12 人工火情已创建/)).toBeVisible()
  await page.getByRole('button', { name: '确认火情' }).click()
  await expect(page.locator('dl').getByText('CONFIRMED', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '创建灭火任务' }).click()
  await expect(page.getByText('灭火任务已创建并进入可靠派发队列')).toBeVisible()
  await page.goto('/tasks')
  await expect
    .poll(
      async () => {
        await page.reload()
        return page.locator('tbody tr').first().innerText()
      },
      { timeout: 20_000 },
    )
    .toContain('SUCCEEDED')
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

test('manual pointer release stops pulses and releases the lease', async ({ page, request }) => {
  await login(page, request)
  const forward = page.getByRole('button', { name: '↑ 前进' })
  await forward.dispatchEvent('pointerdown')
  await expect(page.getByText('租约 HELD')).toBeVisible()
  await page.waitForTimeout(650)
  await page.dispatchEvent('body', 'pointerup')
  await expect(page.getByText('无租约')).toBeVisible()
  await expect(page.getByText(/停止指令已发送/)).toBeVisible()
})

test('quick click cannot start pulses after pointer release', async ({ page, request }) => {
  await login(page, request)
  await page.getByRole('button', { name: '↑ 前进' }).click()
  await page.waitForTimeout(800)
  await expect(page.getByText('无租约')).toBeVisible()
})

test('visibility hidden stops manual pulses and releases the lease', async ({ page, request }) => {
  await login(page, request)
  const forward = page.getByRole('button', { name: '↑ 前进' })
  await forward.dispatchEvent('pointerdown')
  await expect(page.getByText('租约 HELD')).toBeVisible()
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(page.getByText('无租约')).toBeVisible()
  await expect(page.getByText(/停止指令已发送/)).toBeVisible()
})
