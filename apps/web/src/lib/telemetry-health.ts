// Centralized operator-facing severity for topbar telemetry.
// No safety thresholds are hardcoded here; sensor values only reflect
// channel connectivity and alarm state.

export type MetricSeverity = 'normal' | 'active' | 'warning' | 'danger' | 'unknown'

export function linkSeverity(connected: boolean): MetricSeverity {
  return connected ? 'normal' : 'warning'
}

export function robotSeverity(onlineState?: string): MetricSeverity {
  if (onlineState === 'ONLINE') return 'normal'
  if (onlineState === 'STALE') return 'warning'
  if (onlineState === 'OFFLINE') return 'danger'
  return 'unknown'
}

export function batterySeverity(value?: number | null): MetricSeverity {
  if (value == null) return 'unknown'
  if (value >= 30) return 'normal'
  if (value >= 15) return 'warning'
  return 'danger'
}

export function taskSeverity(taskType?: string): MetricSeverity {
  if (!taskType) return 'normal' // 空闲
  if (taskType === 'PATROL' || taskType === 'RETURN_DOCK' || taskType === 'NAVIGATE_TO_PRESET')
    return 'active'
  if (taskType === 'EXTINGUISH') return 'danger'
  return 'unknown'
}

export function localizationSeverity(status?: string): MetricSeverity {
  if (['OK', 'GOOD', 'VALID', 'VALID_SOURCE'].includes(status || '')) return 'normal'
  if (status === 'DEGRADED') return 'warning'
  if (status === 'LOST' || status === 'FAILED') return 'danger'
  return 'unknown'
}

export function channelSeverity(supportState?: string, hasAlarm = false): MetricSeverity {
  if (hasAlarm) return 'danger'
  if (supportState === 'CONNECTED') return 'normal'
  if (supportState === 'STALE') return 'warning'
  if (supportState === 'ERROR') return 'danger'
  if (supportState === 'NOT_CONNECTED') return 'unknown'
  return 'unknown'
}

export function freshnessSeverity(
  ageSeconds: number,
  staleSeconds: number,
  offlineSeconds: number,
): MetricSeverity {
  if (ageSeconds >= offlineSeconds) return 'danger'
  if (ageSeconds >= staleSeconds) return 'warning'
  return 'normal'
}

// 字段级遥测显示：fail-closed。只有明确 CONNECTED 才允许纯正常实时值显示；
// NOT_CONNECTED / UNSUPPORTED / undefined / 其它未知状态一律 --，绝不把历史值当实时值。
export function telemetryValueLabel(
  value: number | null | undefined,
  supportState: string | undefined,
  format: (v: number) => string,
): string {
  if (value == null) return '--'
  if (supportState === 'CONNECTED') return format(value)
  if (supportState === 'STALE') return `数据陈旧 · ${format(value)}`
  if (supportState === 'ERROR') return `数据异常 · ${format(value)}`
  return '--'
}

// 字段级 freshness 退化（与 server apps/api/app/modules/robots/channel_freshness.py 一致）。
// 浏览器拿到 snapshot 后 support_state 不会自行变化，因此必须按当前时间实时重派生。
export interface ChannelFreshnessInput {
  support_state?: string
  last_received_at?: string | null
}

export function effectiveChannelSupportState(
  channel: ChannelFreshnessInput | null | undefined,
  staleSeconds: number | null | undefined,
  offlineSeconds: number | null | undefined,
  nowMs: number,
): string | undefined {
  if (!channel) return undefined
  const state = channel.support_state
  // 显式 ERROR / UNSUPPORTED 等状态不得被时间退化逻辑覆盖
  if (state === 'ERROR' || state === 'UNSUPPORTED') return state
  if (state !== 'CONNECTED' && state !== 'STALE' && state !== 'NOT_CONNECTED') return state
  const last = channel.last_received_at
  if (!last) return state
  const lastMs = Date.parse(last)
  if (!Number.isFinite(lastMs)) return state
  if (staleSeconds == null && offlineSeconds == null) return state
  const ageSeconds = (nowMs - lastMs) / 1000
  if (offlineSeconds != null && ageSeconds >= offlineSeconds) return 'NOT_CONNECTED'
  if (staleSeconds != null && ageSeconds >= staleSeconds) return 'STALE'
  return 'CONNECTED'
}
