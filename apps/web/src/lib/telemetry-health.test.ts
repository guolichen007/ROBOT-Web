import { describe, expect, it } from 'vitest'
import { effectiveChannelSupportState, telemetryValueLabel } from './telemetry-health'

describe('telemetryValueLabel（字段级 freshness 显示语义，fail-closed）', () => {
  const pct = (v: number) => `${v.toFixed(0)}%`
  const smoke = (v: number) => `${v.toFixed(3)}`

  it('CONNECTED 显示实时值', () => {
    expect(telemetryValueLabel(62.3, 'CONNECTED', pct)).toBe('62%')
  })

  it('STALE 明确标记陈旧', () => {
    expect(telemetryValueLabel(62.3, 'STALE', pct)).toBe('数据陈旧 · 62%')
  })

  it('ERROR 明确标记异常', () => {
    expect(telemetryValueLabel(62.3, 'ERROR', pct)).toBe('数据异常 · 62%')
  })

  it('NOT_CONNECTED 不显示历史数值', () => {
    expect(telemetryValueLabel(62.3, 'NOT_CONNECTED', pct)).toBe('--')
  })

  it('UNSUPPORTED 不显示历史数值', () => {
    expect(telemetryValueLabel(62.3, 'UNSUPPORTED', pct)).toBe('--')
  })

  it('supportState undefined（channel 未建立）不显示历史数值', () => {
    expect(telemetryValueLabel(62.3, undefined, pct)).toBe('--')
  })

  it('未知状态不显示历史数值', () => {
    expect(telemetryValueLabel(62.3, 'SOMETHING_ELSE', pct)).toBe('--')
  })

  it('null 值无论状态都显示 --', () => {
    expect(telemetryValueLabel(null, 'CONNECTED', pct)).toBe('--')
    expect(telemetryValueLabel(undefined, 'STALE', pct)).toBe('--')
  })

  it('Smoke STALE → 数据陈旧', () => {
    expect(telemetryValueLabel(0.123, 'STALE', smoke)).toBe('数据陈旧 · 0.123')
  })

  it('Smoke undefined → --（fail-closed）', () => {
    expect(telemetryValueLabel(0.123, undefined, smoke)).toBe('--')
  })
})

describe('effectiveChannelSupportState（与 server channel_freshness.py 一致）', () => {
  const now = 1_700_000_000_000
  const iso = (ms: number) => new Date(ms).toISOString()

  it('CONNECTED + fresh timestamp → CONNECTED', () => {
    expect(
      effectiveChannelSupportState(
        { support_state: 'CONNECTED', last_received_at: iso(now - 2_000) },
        5,
        10,
        now,
      ),
    ).toBe('CONNECTED')
  })

  it('CONNECTED + age > stale → STALE（阈值来自参数，非硬编码 3s）', () => {
    expect(
      effectiveChannelSupportState(
        { support_state: 'CONNECTED', last_received_at: iso(now - 7_000) },
        5,
        10,
        now,
      ),
    ).toBe('STALE')
  })

  it('CONNECTED + age > offline → NOT_CONNECTED', () => {
    expect(
      effectiveChannelSupportState(
        { support_state: 'CONNECTED', last_received_at: iso(now - 12_000) },
        5,
        10,
        now,
      ),
    ).toBe('NOT_CONNECTED')
  })

  it('ERROR 不被时间退化覆盖', () => {
    expect(
      effectiveChannelSupportState(
        { support_state: 'ERROR', last_received_at: iso(now - 12_000) },
        5,
        10,
        now,
      ),
    ).toBe('ERROR')
  })

  it('UNSUPPORTED 不被时间退化覆盖', () => {
    expect(
      effectiveChannelSupportState(
        { support_state: 'UNSUPPORTED', last_received_at: iso(now - 12_000) },
        5,
        10,
        now,
      ),
    ).toBe('UNSUPPORTED')
  })

  it('无 last_received_at → 保留已有 support_state', () => {
    expect(effectiveChannelSupportState({ support_state: 'CONNECTED' }, 5, 10, now)).toBe('CONNECTED')
  })

  it('channel 为 null/undefined → undefined', () => {
    expect(effectiveChannelSupportState(null, 5, 10, now)).toBeUndefined()
  })

  it('threshold 均为 null → 保留已有状态', () => {
    expect(
      effectiveChannelSupportState(
        { support_state: 'CONNECTED', last_received_at: iso(now - 99_000) },
        null,
        null,
        now,
      ),
    ).toBe('CONNECTED')
  })
})
