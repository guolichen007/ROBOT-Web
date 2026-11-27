import { expect, test } from '@playwright/test'
import { loginPage } from './helpers/auth'
import { assertConsoleLayout } from './helpers/console-layout'

// WEB-R1C layout mutation test (opt-in: R1C_LAYOUT_MUTANTS=1). Each mutant injects one CSS
// regression into the REAL console (including the R1B layout the OWNER rejected); the R1C
// geometric contract must reject every one of them, and accept the unmutated page.
test.skip(process.env.R1C_LAYOUT_MUTANTS !== '1', 'opt-in layout mutation run')

const MUTANTS: Record<string, string> = {
  videos_side_by_side:
    '.console-v21 .video-grid{grid-template-columns:repeat(2,minmax(0,1fr))!important;grid-template-rows:none!important}',
  panel_hidden: '#sidebar-console-slot{display:none!important}',
  panel_above_nav: '.sidebar{flex-direction:column-reverse!important}',
  bar_not_pinned: '.yd-monitor-view.console-v21{grid-template-rows:auto minmax(0,1fr) auto 160px!important}',
  map_squeezed:
    '.console-v21 .yd-monitor-core,.console-v21 .yd-monitor-core:has(.console-side){grid-template-columns:minmax(0,1fr) minmax(0,1.2fr)!important}',
  estop_covered:
    '.console-action-bar{position:relative}.console-action-bar::after{content:"";position:absolute;right:0;bottom:0;width:440px;height:90px;z-index:50}',
  horizontal_scroll: '.console-head{min-width:3000px!important}',
  stretched_video: '.video-stage video{object-fit:fill!important}',
  safety_hidden: '[data-testid="console-safety-group"]{visibility:hidden!important}',
}

test('R1C layout contract rejects every injected regression', async ({ page, request }) => {
  test.setTimeout(150_000)
  await loginPage(page, request)
  const viewport = { width: 1366, height: 768 }
  await page.setViewportSize(viewport)
  await expect(page.getByTestId('console-control-panel')).toBeVisible()
  await assertConsoleLayout(page, viewport) // baseline must pass
  const verdicts: string[] = []
  for (const [name, css] of Object.entries(MUTANTS)) {
    await page.goto('/monitor')
    await expect(page.getByTestId('console-control-panel')).toBeAttached()
    await page.addStyleTag({ content: css })
    await page.waitForTimeout(400)
    let killed = false
    let why = ''
    try {
      await assertConsoleLayout(page, viewport)
    } catch (error) {
      killed = true
      why =
        String((error as Error).message)
          .split('\n')
          .find((line) => line.trim()) || ''
    }
    verdicts.push(`${name} => ${killed ? `KILLED :: ${why}` : 'SURVIVED'}`)
  }
  console.log(verdicts.join('\n'))
  expect(
    verdicts.filter((v) => v.includes('SURVIVED')),
    verdicts.join('\n'),
  ).toEqual([])
})
