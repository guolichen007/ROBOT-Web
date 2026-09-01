import { describe, expect, it } from 'vitest'
import { telemetryValueLabel } from './telemetry-health'

describe('telemetryValueLabel（字段级 freshness 显示语义）', () => {
  const pct = (v: number) => `${v.toFixed(0)}%`

  it('CONNECTED 显示实时值', () => {
    expect(telemetryValueLabel(62.3, 'CONNECTED', pct)).toBe('62%')
  })

  it('STALE 明确标记陈旧，绝不冒充实时正常值', () => {
    expect(telemetryValueLabel(62.3, 'STALE', pct)).toBe('数据陈旧 · 62%')
  })

  it('NOT_CONNECTED 不显示历史数值', () => {
    expect(telemetryValueLabel(62.3, 'NOT_CONNECTED', pct)).toBe('--')
  })

  it('null 值无论状态都显示 --', () => {
    expect(telemetryValueLabel(null, 'CONNECTED', pct)).toBe('--')
    expect(telemetryValueLabel(undefined, 'STALE', pct)).toBe('--')
  })

  it('channel 尚未建立（无 freshness 信息）仍显示值', () => {
    expect(telemetryValueLabel(62.3, undefined, pct)).toBe('62%')
  })
})
