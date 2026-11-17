import { expect, test } from '@playwright/test'
import { loginPage } from './helpers/auth'
import { ensureRobotIdle } from './helpers/robot-state'
import { assertControlDockVisible } from './helpers/control-dock'

// 当前监控布局（current monitor layout）：只校验当前稳定合同，不复刻历史 ui-gate2 几何常量。
// 真实 emergency_stop / reset_estop 未实现，软件急停这里只校验可见性，不做锁存/复位流程。
// 不再包含 alarm smoke browser case：/alarms/manual 创建的是 site-level FireEvent（robot_id=null），
// Monitor 的 activeRobotAlarms 按 selected robot.id 过滤，且 severity=HIGH 不会触发 production
// critical banner（只 primaryAlarm.severity===CRITICAL）。该 case 是 domain contract 本身错误，不是 selector 过时。

test('current monitor layout is stable across common resolutions', async ({ page, request }) => {
  await ensureRobotIdle(request, 'R001')
  await loginPage(page, request)
  for (const viewport of [
    { width: 2048, height: 997 },
    { width: 1920, height: 1080 },
    { width: 1680, height: 900 },
  ]) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height })

    // 当前稳定合同：主地图 + 车顶实时相机 + dock（state-dependent 动态标签）
    await expect(page.getByRole('heading', { name: '停车场二维地图' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '监控相机', exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: '红外热成像', exact: true })).toBeVisible()
    await assertControlDockVisible(page)

    // 关键区域不溢出、无横向滚动、控制区不被裁切
    const meta = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    }))
    expect(meta.scrollWidth).toBeLessThanOrEqual(meta.innerWidth + 2)
    const dock = await page.locator('.operations-command-dock').boundingBox()
    expect(dock).not.toBeNull()
    expect(dock!.y + dock!.height).toBeLessThanOrEqual(viewport.height + 1)
  }
})

// G3-R2：取消独立温度卡片后首页四项指标（状态 / 电池 / 烟雾 / 通信）在桌面单行、移动端 2×2，
// 双路视频（RGB + 热成像）仍同时挂载；KPI 区与视频区不横向溢出视口。
test('four KPI cards without temperature keep desktop and mobile layout', async ({ page, request }) => {
  await ensureRobotIdle(request, 'R001')
  await loginPage(page, request)
  for (const viewport of [
    { width: 1920, height: 1080, columns: 4 },
    { width: 1366, height: 768, columns: 4 },
    { width: 1024, height: 768, columns: 2 },
    { width: 390, height: 844, columns: 2 },
  ]) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    const kpis = page.locator('.hmi-kpi')
    await expect(kpis).toHaveCount(4)
    await expect(page.locator('[data-kpi="temperature"]')).toHaveCount(0)
    await expect(page.locator('.hmi-kpis')).not.toContainText('温度')
    await expect(page.locator('.hmi-kpis')).not.toContainText('℃')
    await expect(page.locator('.hmi-camera')).toHaveCount(2)
    const boxes = []
    for (const card of await kpis.all()) boxes.push((await card.boundingBox())!)
    const rows = new Set(boxes.map((b) => Math.round(b.y)))
    expect(rows.size).toBe(4 / viewport.columns)
    for (const b of boxes) {
      expect(b.width).toBeGreaterThan(60)
      expect(b.x).toBeGreaterThanOrEqual(0)
      expect(b.x + b.width).toBeLessThanOrEqual(viewport.width + 1)
    }
    const video = (await page.locator('.video-surveillance').boundingBox())!
    expect(video.width).toBeGreaterThan(150)
    expect(video.x + video.width).toBeLessThanOrEqual(viewport.width + 1)
  }
})
