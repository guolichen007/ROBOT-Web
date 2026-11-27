import { expect, test } from '@playwright/test'
import { loginPage } from './helpers/auth'

// WEB-R1C R1C-2 real-browser counterexample (opt-in: R1C_READINESS_E2E=1): fast vehicle switch
// A→B→A while the FIRST A readiness answer is held back at the network layer and finally released
// saying "everything allowed". The console must keep using only the newest A answer (frozen).
// Also: when readiness requests fail, every control in the task bar and left panel is disabled.
test.skip(process.env.R1C_READINESS_E2E !== '1', 'opt-in readiness race run')

test('A→B→A with a late permissive answer never enables controls; failure disables all', async ({
  page,
  request,
}) => {
  test.setTimeout(90_000)
  await loginPage(page, request)
  const sel = page.getByTestId('console-vehicle')
  await expect(sel).toBeVisible()
  const options = await sel
    .locator('option')
    .evaluateAll((els) => els.map((e) => (e as HTMLOptionElement).value))
  expect(options.length).toBeGreaterThanOrEqual(2)
  const [A, B] = options.includes('R001') && options.includes('R002') ? ['R001', 'R002'] : options
  await sel.selectOption(B)
  await page.waitForTimeout(800)

  let firstA: null | ((body: string) => Promise<void>) = null
  let aCount = 0
  await page.route(/\/api\/v1\/robots\/[^/]+\/operation-readiness$/, async (route) => {
    const url = route.request().url()
    const real = await route.fetch()
    const body = await real.json()
    if (url.includes(`/robots/${A}/`)) {
      aCount += 1
      if (aCount === 1) {
        // hold the first A answer, and forge it to "everything allowed" when released late
        const forged = {
          ...body,
          control_frozen: false,
          freeze_scope: null,
          freeze_reasons: [],
          actions: Object.fromEntries(
            Object.entries(body.actions).map(([k, v]) => [
              k,
              { ...(v as object), allowed: true, reasons: [] },
            ]),
          ),
        }
        await new Promise<void>((resolve) => {
          firstA = async () => {
            await route.fulfill({ response: real, json: forged })
            resolve()
          }
        })
        return
      }
    }
    await route.fulfill({ response: real, json: body })
  })

  await sel.selectOption(A) // first A request (held)
  await sel.selectOption(B)
  await sel.selectOption(A) // second A request (real answer: frozen in this run)
  await expect(page.locator('[data-dim="ready"]')).toContainText(/已冻结|未就绪/, { timeout: 10_000 })
  expect(firstA).not.toBeNull()
  // release the stale permissive answer and sample the bar continuously for 900 ms (shorter than
  // the 1 s poll interval, so a newer poll cannot mask an applied stale answer)
  const bar = page.getByTestId('console-action-bar')
  // the newest A answer (frozen) is applied at this point; only now release the stale one
  const release = firstA!('')
  const enabledSeen: string[] = []
  const until = Date.now() + 900
  while (Date.now() < until) {
    const enabled = await bar.evaluate((el) =>
      [...el.querySelectorAll('button')]
        .filter((b) => !(b as HTMLButtonElement).disabled)
        .map((b) => b.textContent?.trim() || ''),
    )
    enabledSeen.push(...enabled)
    await page.waitForTimeout(40)
  }
  await release
  console.log(`R1C_RACE enabled_after_stale_release=${JSON.stringify([...new Set(enabledSeen)])}`)
  expect(enabledSeen, 'a stale A answer enabled controls after A→B→A').toEqual([])
  for (const b of await bar.getByRole('button').all()) await expect(b, await b.innerText()).toBeDisabled()
  await expect(page.locator('[data-dim="ready"]')).toContainText(/已冻结|未就绪/)

  // readiness endpoint failing → nothing usable, explicit reason
  await page.unroute(/\/api\/v1\/robots\/[^/]+\/operation-readiness$/)
  await page.route(/\/api\/v1\/robots\/[^/]+\/operation-readiness$/, (route) =>
    route.fulfill({ status: 503, body: '{}' }),
  )
  await expect(bar).toContainText('控制资格查询失败', { timeout: 5000 })
  for (const b of await bar.getByRole('button').all()) await expect(b, await b.innerText()).toBeDisabled()
  for (const b of await page
    .getByTestId('console-control-panel')
    .locator('.device-actions button, [data-action]')
    .all())
    await expect(b).toBeDisabled()
})
