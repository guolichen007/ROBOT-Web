import type { Alarm, DataSupportState, OnlineState } from '@/types'

export type SituationState = 'FIRE_CRITICAL' | 'DEGRADED' | 'NORMAL' | 'OFFLINE_UNKNOWN'

const severityRank: Record<string, number> = { CRITICAL: 4, HIGH: 3, MEDIUM: 2, LOW: 1 }
const stateRank: Record<string, number> = { NEW: 4, ACKNOWLEDGED: 3, CONFIRMED: 2 }

export function compareOperationalAlarms(left: Alarm, right: Alarm): number {
  return (
    (severityRank[right.severity] || 0) - (severityRank[left.severity] || 0) ||
    (stateRank[right.state] || 0) - (stateRank[left.state] || 0) ||
    new Date(right.last_seen_at).getTime() - new Date(left.last_seen_at).getTime()
  )
}

export function operationalSituation(input: {
  criticalFire: boolean
  websocketConnected: boolean
  onlineState?: OnlineState
  localizationStatus?: string
  estopActive?: boolean | null
  estopSupport: DataSupportState
}): SituationState {
  if (input.criticalFire) return 'FIRE_CRITICAL'
  if (!input.websocketConnected || input.onlineState !== 'ONLINE') return 'OFFLINE_UNKNOWN'
  if (
    input.estopActive !== false ||
    input.estopSupport !== 'CONNECTED' ||
    !['OK', 'GOOD', 'VALID', 'VALID_SOURCE'].includes(input.localizationStatus || '')
  )
    return 'DEGRADED'
  return 'NORMAL'
}
