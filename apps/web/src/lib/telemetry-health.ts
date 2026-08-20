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
  if (taskType === 'PATROL' || taskType === 'RETURN_DOCK' || taskType === 'NAVIGATE_TO_PRESET') return 'active'
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

export function freshnessSeverity(ageSeconds: number, staleSeconds: number, offlineSeconds: number): MetricSeverity {
  if (ageSeconds >= offlineSeconds) return 'danger'
  if (ageSeconds >= staleSeconds) return 'warning'
  return 'normal'
}
