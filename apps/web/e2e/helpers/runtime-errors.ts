import type { Page } from '@playwright/test'

const RUNTIME_ERROR_MARKERS = [
  'Cannot read properties',
  'emitsOptions',
  'Unhandled error during execution',
  'TypeError',
]

// 收集真实 JS / Vue runtime exception（pageerror + 匹配关键标记的 console.error）。
// 忽略 401 refresh、媒体测试 HTTP 等预期资源错误，避免机械判失败。
export function collectRuntimeErrors(page: Page): () => string[] {
  const errors: string[] = []
  page.on('pageerror', (error) => {
    errors.push(`pageerror: ${error.message}`)
  })
  page.on('console', (message) => {
    if (message.type() !== 'error') return
    const text = message.text()
    if (text.includes('401') || text.includes('Failed to load resource')) return
    if (RUNTIME_ERROR_MARKERS.some((marker) => text.includes(marker))) {
      errors.push(`console: ${text}`)
    }
  })
  return () => errors
}
