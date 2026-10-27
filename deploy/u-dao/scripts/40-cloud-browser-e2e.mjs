// U-Dao 部署栈浏览器 E2E（bootstrap 证书 + COOKIE_SECURE=true 的完整登录/HMI/媒体链）。
// 运行环境：playwright 容器镜像（mcr.microsoft.com/playwright:v1.62.1-noble）或本机 node+playwright。
// 用法：
//   node deploy/u-dao/scripts/40-cloud-browser-e2e.mjs \
//     --base-url https://firebot.local:18443 \
//     --admin-password-file /srv/u-dao/projects/firebot/runtime/secrets/app/bootstrap_admin_password
// 断言：登录（强制改密后）→ HMI 加载 → VideoCard 用户点击 → WHEP LIVE + 真实 ICE 候选对连通。
// 注意：首次验收会把 bootstrap admin 密码轮换（新值写回 runtime secret 文件，600，不打印）。
import { readFileSync, writeFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { chromium } from 'playwright'

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, cur, i, arr) => {
    if (cur.startsWith('--')) acc[cur.slice(2)] = arr[i + 1]
    return acc
  }, {}),
)
const baseUrl = args['base-url'] || 'https://firebot.local:18443'
const passwordFile = args['admin-password-file']
const adminPassword = readFileSync(passwordFile, 'utf8').trim()
const nextPassword = `u-dao-accept-${randomBytes(12).toString('hex')}`

const failures = []
const check = (name, cond, detail = '') => {
  if (cond) console.log(`PASS ${name}`)
  else {
    failures.push(name)
    console.log(`FAIL ${name} ${detail}`)
  }
}

const browser = await chromium.launch()
const context = await browser.newContext({
  ignoreHTTPSErrors: true, // BOOTSTRAP_LOCAL_ONLY 自签证书（验收专用；正式证书由 OWNER 替换）
  baseURL: baseUrl,
})
// 捕获 RTCPeerConnection 实例：LIVE 后用 getStats 断言真实 ICE 候选对连通（不满足于 HTTP 201）
await context.addInitScript(() => {
  window.__peers = []
  const Orig = window.RTCPeerConnection
  window.RTCPeerConnection = function (...args) {
    const pc = new Orig(...args)
    window.__peers.push(pc)
    return pc
  }
  window.RTCPeerConnection.prototype = Orig.prototype
})
const page = await context.newPage()

// 1) 登录（COOKIE_SECURE=true 必须走 https）
await page.goto('/login')
await page.getByPlaceholder('请输入账号').fill('admin')
await page.getByPlaceholder('请输入密码').fill(adminPassword)
await page.getByRole('button', { name: '登录' }).click()
await page.waitForURL(/change-password|robots/, { timeout: 15000 })
check('登录成功（COOKIE_SECURE）', /change-password|robots/.test(page.url()), page.url())

// 2) 强制改密（bootstrap admin 的 must_change_password 语义）
if (page.url().includes('change-password')) {
  await page.getByPlaceholder('请输入当前密码').fill(adminPassword)
  await page.getByPlaceholder('至少 12 位').fill(nextPassword)
  await page.getByPlaceholder('再次输入新密码').fill(nextPassword)
  await page.getByRole('button', { name: /确认|修改|保存/ }).click()
  await page.waitForURL('**/robots', { timeout: 15000 }).catch(() => null)
  check('改密后进入 robots 页', page.url().includes('/robots'), page.url())
  writeFileSync(passwordFile, nextPassword + '\n', { mode: 0o600 }) // 轮换后的真值仍以 runtime 文件为准
}

// 3) HMI 加载
await page.goto('/monitor')
await page.waitForTimeout(2000)
const body = (await page.locator('body').textContent()) || ''
check('HMI MonitorView 渲染', body.length > 500, `len=${body.length}`)

// 4) 媒体链：用户主动点击「连接视频」→ WHEP → LIVE → 真实 ICE 候选对
const connectButton = page.locator('button', { hasText: '连接视频' }).first()
const hasButton = (await connectButton.count()) > 0
if (hasButton) {
  await connectButton.click()
  const live = await page
    .waitForSelector('.video-live-tag', { timeout: 20000 })
    .then(() => true)
    .catch(() => false)
  check('VideoCard WHEP 会话进入 LIVE', live,
    live ? '' : (await page.locator('.video-placeholder').textContent().catch(() => '')))
  if (live) {
    const iceOk = await page.evaluate(async () => {
      for (const pc of window.__peers) {
        const stats = await pc.getStats()
        for (const report of stats.values()) {
          if (report.type === 'candidate-pair' && report.state === 'succeeded') return true
        }
      }
      return false
    })
    check('真实 ICE 候选对连通（candidate-pair succeeded）', iceOk)
  }
} else {
  check('VideoCard 连接视频按钮存在', false, '未找到按钮；确认 compose.media-e2e 已启动且 R001 流已注册')
}

await browser.close()
if (failures.length) {
  console.error(`CLOUD_BROWSER_E2E=FAIL count=${failures.length}`)
  process.exit(1)
}
console.log('CLOUD_BROWSER_E2E=PASS')
