/* global process */
// Shared loopback-only admin login for the R1C real-browser scripts (cookies land in `context`).
// Handles the forced first-login password change exactly like e2e/helpers/auth.ts.
const CHANGED = 'Firebot-E2E-Changed-2026!'
export async function login(context, page) {
  const candidates = [
    ...new Set([CHANGED, process.env.E2E_ADMIN_PASSWORD, process.env.BOOTSTRAP_ADMIN_PASSWORD]),
  ].filter(Boolean)
  for (const pw of candidates) {
    const r = await page.request.post('/api/v1/auth/login', { data: { username: 'admin', password: pw } })
    if (!r.ok()) continue
    const body = await r.json()
    if (!body.user.must_change_password) return body.access_token
    const csrf = (await context.cookies()).find((c) => c.name === 'csrf_token')?.value || ''
    const c = await page.request.post('/api/v1/auth/change-password', {
      data: { current_password: pw, new_password: CHANGED },
      headers: { Authorization: `Bearer ${body.access_token}`, 'X-CSRF-Token': csrf },
    })
    if (!c.ok()) throw new Error(`change-password failed ${c.status()}`)
    const again = await page.request.post('/api/v1/auth/login', {
      data: { username: 'admin', password: CHANGED },
    })
    if (!again.ok()) throw new Error(`relogin failed ${again.status()}`)
    return (await again.json()).access_token
  }
  throw new Error('no working admin password')
}
