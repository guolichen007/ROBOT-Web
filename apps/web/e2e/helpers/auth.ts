import { expect, type APIRequestContext, type APIResponse, type Page } from '@playwright/test'

// 统一 E2E 认证语义：所有 spec 复用，禁止各自复制 bootstrap-password 逻辑。
// 候选顺序：E2E_CHANGED_PASSWORD → E2E_ADMIN_PASSWORD → Firebot-Dev-2026!（旧本地兼容）。
// 若 bootstrap 登录后 must_change_password=true，先 change-password 再用新密码重登。
// 所有失败信息只含 status/detail（服务端已脱敏），绝不打印 password/token/cookie。

const changedPassword = process.env.E2E_CHANGED_PASSWORD || 'Firebot-E2E-Changed-2026!'
const adminPassword = process.env.E2E_ADMIN_PASSWORD
const devFallback = 'Firebot-Dev-2026!'

async function safeDetail(response: APIResponse): Promise<unknown> {
  try {
    const body = (await response.json()) as { detail?: unknown; message?: unknown }
    return body.detail ?? body.message ?? null
  } catch {
    return null
  }
}

export async function workingPassword(
  request: APIRequestContext,
): Promise<{ value: string; mustChange: boolean }> {
  const candidates = [...new Set([changedPassword, adminPassword, devFallback].filter((v): v is string => Boolean(v)))]
  let lastStatus: number | undefined
  let lastDetail: unknown
  for (const candidate of candidates) {
    const response = await request.post('/api/v1/auth/login', {
      data: { username: 'admin', password: candidate },
    })
    if (response.ok()) {
      const body = (await response.json()) as { user: { must_change_password: boolean } }
      return { value: candidate, mustChange: Boolean(body.user.must_change_password) }
    }
    lastStatus = response.status()
    lastDetail = await safeDetail(response)
  }
  throw new Error(
    `No valid E2E admin password (last status=${lastStatus ?? 'transport-error'}, detail=${JSON.stringify(lastDetail ?? null)})`,
  )
}

export async function ensurePasswordReady(request: APIRequestContext): Promise<string> {
  const credentials = await workingPassword(request)
  if (!credentials.mustChange) {
    const login = await request.post('/api/v1/auth/login', {
      data: { username: 'admin', password: credentials.value },
    })
    if (!login.ok()) throw new Error(`login failed status=${login.status()}`)
    return (await login.json()).access_token
  }
  const login = await request.post('/api/v1/auth/login', {
    data: { username: 'admin', password: credentials.value },
  })
  if (!login.ok()) throw new Error(`login failed status=${login.status()}`)
  const accessToken = (await login.json()).access_token
  const change = await request.post('/api/v1/auth/change-password', {
    headers: { Authorization: `Bearer ${accessToken}` },
    data: { current_password: credentials.value, new_password: changedPassword },
  })
  if (!change.ok()) throw new Error(`change-password failed status=${change.status()}`)
  const relogin = await request.post('/api/v1/auth/login', {
    data: { username: 'admin', password: changedPassword },
  })
  if (!relogin.ok()) throw new Error(`relogin failed status=${relogin.status()}`)
  return (await relogin.json()).access_token
}

export async function getAccessToken(request: APIRequestContext): Promise<string> {
  return ensurePasswordReady(request)
}

export async function loginPage(page: Page, request: APIRequestContext): Promise<void> {
  const credentials = await workingPassword(request)
  await page.goto('/login')
  await page.locator('#login-username').fill('admin')
  await page.locator('#login-password').fill(credentials.value)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  if (credentials.mustChange) {
    await page.locator('#cp-current').fill(credentials.value)
    await page.locator('#cp-new').fill(changedPassword)
    await page.locator('#cp-confirm').fill(changedPassword)
    await page.getByRole('button', { name: '修改并重新登录' }).click()
    await expect(page).toHaveURL(/\/login$/)
    await page.locator('#login-username').fill('admin')
    await page.locator('#login-password').fill(changedPassword)
    await page.getByRole('button', { name: '登录', exact: true }).click()
  }
  await expect(page.getByRole('heading', { name: '停车场巡检地图' })).toBeVisible()
}
