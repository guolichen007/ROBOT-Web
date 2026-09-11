import { expect, type Page } from '@playwright/test'

// OperationsCommandDock 的 state-dependent 动态标签合同。
// 不锁死瞬时状态，只验证 production 明确定义的合法 label 集合。
const PATROL_LABELS = /^(开始巡检|继续巡检|● 巡检中|正在启动…)$/
const STOP_LABELS = /^(停止|已停止|● 停止中)$/
const HOME_LABELS = /^(返回等待区|已在等待区|继续返回|● 返回中)$/

export async function assertControlDockVisible(page: Page): Promise<void> {
  const dock = page.locator('.operations-command-dock')
  await expect(dock).toBeVisible()

  const buttonTexts = await dock.getByRole('button').allTextContents()
  const normalized = buttonTexts.map((text) => text.replace(/\s+/g, ' ').trim())

  const matchAny = (re: RegExp) => normalized.some((text) => re.test(text))
  expect(matchAny(PATROL_LABELS), `patrol button label not legal, got ${JSON.stringify(normalized)}`).toBe(
    true,
  )
  expect(matchAny(STOP_LABELS), `stop button label not legal, got ${JSON.stringify(normalized)}`).toBe(true)
  expect(matchAny(HOME_LABELS), `home button label not legal, got ${JSON.stringify(normalized)}`).toBe(true)

  // 软件急停 exact（真实 estop 未实现，仅可见性）
  await expect(dock.getByRole('button', { name: '软件急停', exact: true })).toBeVisible()
}
