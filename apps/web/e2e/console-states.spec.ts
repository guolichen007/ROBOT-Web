import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { getAccessToken, loginPage } from './helpers/auth'
import { CONSOLE_VIEWPORTS, assertConsoleLayout } from './helpers/console-layout'

// WEB-R1C real-browser state matrix (opt-in: R1C_STATES=normal,patrol,fire,offline | frozen).
// Every state is produced by the REAL isolated stack (API + PostgreSQL + Redis + mosquitto + ingress
// + dispatcher + task-worker + mock-robot), never by front-end stubs. Each state: full R1C layout
// geometry at 3 viewports + screenshot + server-side state JSON.
const STATES = (process.env.R1C_STATES || '').split(',').filter(Boolean)
test.skip(!STATES.length, 'opt-in R1C state matrix')
const OUT = process.env.R1C_OUT || 'artifacts/r1c-states'
const VEHICLE = process.env.CONSOLE_VEHICLE || 'R001'

let cachedToken = ''
async function api(request: APIRequestContext, method: string, path: string, data?: unknown) {
  const token = cachedToken || (cachedToken = await getAccessToken(request))
  const r = await request.fetch(`/api/v1${path}`, {
    method,
    data,
    headers: { Authorization: `Bearer ${token}`, 'Idempotency-Key': crypto.randomUUID() },
  })
  return { status: r.status(), body: await r.json().catch(() => null) }
}
async function robotState(request: APIRequestContext) {
  const snap = (await api(request, 'GET', '/monitor/snapshot')).body
  const r = snap.robots.find((x: { vehicle_id: string }) => x.vehicle_id === VEHICLE)
  return {
    vehicle_id: r?.vehicle_id,
    online_state: r?.online_state,
    estop_active: r?.estop_active,
    boot_id: r?.boot_id,
    pose: { x: r?.x, y: r?.y, theta: r?.theta },
    server_received_at: r?.server_received_at,
    operation_readiness: r?.operation_readiness && {
      control_frozen: r.operation_readiness.control_frozen,
      freeze_reasons: r.operation_readiness.freeze_reasons,
      allowed: Object.entries(r.operation_readiness.actions)
        .filter(([, v]) => (v as { allowed: boolean }).allowed)
        .map(([k]) => k),
    },
    active_tasks: snap.tasks
      .filter(
        (t: { robot_id: string; status: string }) =>
          t.robot_id === r?.id && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(t.status),
      )
      .map((t: { type: string; status: string }) => ({ type: t.type, status: t.status })),
    active_alarms: snap.alarms
      .filter((a: { state: string }) => !['RESOLVED', 'CLOSED', 'DISMISSED'].includes(a.state))
      .map((a: { event_code: string; severity: string; state: string }) => ({
        event_code: a.event_code,
        severity: a.severity,
        state: a.state,
      })),
  }
}
async function capture(
  page: Page,
  request: APIRequestContext,
  state: string,
  check?: (page: Page) => Promise<void>,
) {
  mkdirSync(OUT, { recursive: true })
  const shots = []
  for (const viewport of CONSOLE_VIEWPORTS) {
    await page.setViewportSize(viewport)
    await page.waitForTimeout(700)
    await assertConsoleLayout(page, viewport)
    if (check) await check(page)
    const file = `r1c_${state}_${VEHICLE}_${viewport.width}x${viewport.height}.png`
    await page.screenshot({ path: join(OUT, file) })
    shots.push({
      viewport: `${viewport.width}x${viewport.height}`,
      file,
      status: (await page.getByTestId('console-status').innerText()).replace(/\s+/g, ' '),
      task_bar: (await page.getByTestId('console-action-bar').innerText()).replace(/\s+/g, ' '),
    })
  }
  const record = {
    state,
    vehicle: VEHICLE,
    captured_at: new Date().toISOString(),
    server: await robotState(request),
    shots,
  }
  writeFileSync(join(OUT, `r1c_state_${state}_${VEHICLE}.json`), JSON.stringify(record, null, 2))
}

test('R1C console state matrix (real stack)', async ({ page, request }) => {
  test.setTimeout(170_000)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await loginPage(page, request) // first: performs the forced first-login password change
  if (!(STATES.length === 1 && STATES[0] === 'offline'))
    await expect
      .poll(async () => (await robotState(request)).online_state, { timeout: 20_000 })
      .toBe('ONLINE')
  await expect(page.getByTestId('console-control-panel')).toBeVisible()
  const sel = page.getByTestId('console-vehicle')
  if ((await sel.inputValue()) !== VEHICLE) await sel.selectOption(VEHICLE)

  if (STATES.includes('normal')) {
    await expect(page.locator('[data-dim="link"]')).toContainText('实时')
    await capture(page, request, 'normal')
  }
  if (STATES.includes('frozen')) {
    await expect(page.locator('[data-dim="ready"]')).toContainText('已冻结', { timeout: 15_000 })
    await capture(page, request, 'frozen', async (p) => {
      for (const b of await p.getByTestId('console-action-bar').getByRole('button').all())
        await expect(b, await b.innerText()).toBeDisabled()
      await expect(p.getByTestId('console-action-bar')).toContainText('控制已冻结')
    })
  }
  if (STATES.includes('patrol')) {
    const snap = (await api(request, 'GET', '/monitor/snapshot')).body
    const rid = snap.robots.find((x: { vehicle_id: string }) => x.vehicle_id === VEHICLE)?.id
    const plans = ((await api(request, 'GET', '/patrol-plans')).body || []) as Array<{
      id: string
      robot_id: string
      execution_mode?: string
    }>
    const plan = plans.find((p) => p.robot_id === rid && p.execution_mode !== 'PREVIEW_ONLY')
    expect(plan, 'executable patrol plan in the test seed').toBeTruthy()
    const created = await api(request, 'POST', '/tasks/patrol-plan', {
      robot_id: VEHICLE,
      patrol_plan_id: plan!.id,
    })
    expect(created.status, JSON.stringify(created.body)).toBeLessThan(300)
    await expect
      .poll(async () => (await robotState(request)).active_tasks.length, { timeout: 15_000 })
      .toBeGreaterThan(0)
    await expect(page.getByTestId('console-task-group')).toContainText('巡检中', { timeout: 15_000 })
    await capture(page, request, 'patrol', async (p) => {
      await expect(p.getByTestId('console-safety-group').locator('.dock-stop')).toBeEnabled()
    })
  }
  if (STATES.includes('fire')) {
    const snap = (await api(request, 'GET', '/monitor/snapshot')).body
    const slot = snap.parking_slots[0]
    const fire = await api(request, 'POST', '/alarms/manual', {
      parking_slot_id: slot.id,
      fire_type: 'smoke',
      note: 'R1C 隔离栈火警状态截图',
      map_version: snap.map_version.version,
      severity: 'CRITICAL',
      media: {},
    })
    expect(fire.status, JSON.stringify(fire.body)).toBeLessThan(300)
    await expect(page.getByTestId('alarm-popup')).toBeVisible({ timeout: 15_000 })
    await capture(page, request, 'fire', async (p) => {
      await expect(p.getByTestId('alarm-popup')).toBeVisible()
    })
  }
  if (STATES.includes('offline')) {
    // the caller stops every vehicle-side process; wait for the server to declare it
    await expect
      .poll(async () => (await robotState(request)).online_state, { timeout: 40_000, intervals: [1000] })
      .toMatch(/OFFLINE|STALE/)
    await page.waitForTimeout(11_000)
    await capture(page, request, 'offline', async (p) => {
      await expect(p.locator('[data-dim="link"]')).toContainText(/离线|陈旧/)
      await expect(p.getByTestId('console-pose')).toContainText(/未知|旧数据/)
      // vehicle offline: every task button is unavailable whatever its label (a patrol the server
      // still tracks as active shows 巡检中 — that is the real server state, not a UI default)
      for (const b of await p.getByTestId('console-task-group').getByRole('button').all())
        await expect(b, await b.innerText()).toBeDisabled()
    })
  }
  expect(errors).toEqual([])
})
