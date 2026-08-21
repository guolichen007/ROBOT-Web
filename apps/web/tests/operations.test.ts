import { describe, expect, it } from 'vitest'
import { compareOperationalAlarms, operationalSituation } from '@/lib/operations'
import type { Alarm } from '@/types'

function alarm(severity: string, state: string, last_seen_at: string): Alarm {
  return {
    id: `${severity}-${state}`,
    robot_id: 'robot-a',
    event_code: 'FE-TEST',
    severity,
    state,
    last_seen_at,
    fire_type: 'smoke',
    occurrence_count: 1,
    detection_method: 'AUTO',
  }
}

describe('industrial HMI operational semantics', () => {
  it('sorts alarms by severity, active state and then recency', () => {
    const rows = [
      alarm('HIGH', 'NEW', '2026-08-13T10:00:00Z'),
      alarm('CRITICAL', 'ACKNOWLEDGED', '2026-08-13T09:00:00Z'),
      alarm('CRITICAL', 'NEW', '2026-08-13T08:00:00Z'),
    ].sort(compareOperationalAlarms)
    expect(rows.map((item) => `${item.severity}:${item.state}`)).toEqual([
      'CRITICAL:NEW',
      'CRITICAL:ACKNOWLEDGED',
      'HIGH:NEW',
    ])
  })

  it('never reports normal when realtime, localization or estop truth is unknown', () => {
    expect(
      operationalSituation({
        criticalFire: false,
        websocketConnected: true,
        onlineState: 'OFFLINE',
        localizationStatus: 'GOOD',
        estopActive: false,
        estopSupport: 'CONNECTED',
      }),
    ).toBe('OFFLINE_UNKNOWN')
    expect(
      operationalSituation({
        criticalFire: false,
        websocketConnected: true,
        onlineState: 'ONLINE',
        localizationStatus: 'VALID_SOURCE',
        estopActive: null,
        estopSupport: 'UNSUPPORTED',
      }),
    ).toBe('DEGRADED')
  })

  it('reports a distinct ESTOP_ACTIVE situation when the latch is set', () => {
    expect(
      operationalSituation({
        criticalFire: false,
        websocketConnected: true,
        onlineState: 'ONLINE',
        localizationStatus: 'VALID_SOURCE',
        estopActive: true,
        estopSupport: 'CONNECTED',
      }),
    ).toBe('ESTOP_ACTIVE')
  })
})
