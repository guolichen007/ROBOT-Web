import { expect, test } from '@playwright/test'
import { loginPage } from './helpers/auth'

// 当前监控布局（current monitor layout）：只校验当前稳定合同，不复刻历史 ui-gate2 几何常量。
// 真实 emergency_stop / reset_estop 未实现，软件急停这里只校验可见性，不做锁存/复位流程。
// 不再包含 alarm smoke browser case：/alarms/manual 创建的是 site-level FireEvent（robot_id=null），
// Monitor 的 activeRobotAlarms 按 selected robot.id 过滤，且 severity=HIGH 不会触发 production
// critical banner（只 primaryAlarm.severity===CRITICAL）。该 case 是 domain contract 本身错误，不是 selector 过时。

test('current monitor layout is stable across common resolutions', async ({ page, request }) => {
  await loginPage(page, request)
  for (const viewport of [
    { width: 2048, height: 997 },
    { width: 1920, height: 1080 },
    { width: 1366, height: 768 },
  ]) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height })

    // 当前稳定合同：主地图 + 车顶实时相机 + 控制区 + 三个运动控制 + 软件急停可见性
    await expect(page.getByRole('heading', { name: '停车场巡检地图' })).toBeVisible()
    await expect(page.getByText('车顶实时相机').first()).toBeVisible()
    await expect(page.locator('.operations-command-dock')).toBeVisible()
    await expect(page.getByRole('button', { name: '开始巡检' })).toBeVisible()
    await expect(page.getByRole('button', { name: '停止' })).toBeVisible()
    await expect(page.getByRole('button', { name: '返回等待区' })).toBeVisible()
    // 软件急停平台仍展示；真实 emergency_stop/reset_estop 未实现，这里只校验可见性
    await expect(page.getByRole('button', { name: '软件急停' })).toBeVisible()

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
