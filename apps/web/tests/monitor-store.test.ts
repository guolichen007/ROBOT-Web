import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { useMonitorStore } from '@/stores/monitor'

describe('monitor realtime state', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('applies robot deltas and advances the watermark', () => {
    const store = useMonitorStore()
    store.applyEvent({
      stream_id: '171-0',
      event_type: 'robot.location',
      data: { vehicle_id: 'R001', x: 3, y: 4 },
    })
    expect(store.robot?.x).toBe(3)
    expect(store.lastStreamId).toBe('171-0')
  })

  it('upserts alarms by durable id', () => {
    const store = useMonitorStore()
    const alarm = {
      id: 'a1',
      event_code: 'FE-1',
      state: 'NEW',
      severity: 'HIGH',
      fire_type: 'smoke',
      occurrence_count: 1,
      detection_method: 'AUTO',
      last_seen_at: '2026-08-10T00:00:00Z',
    }
    store.applyEvent({ stream_id: '172-0', event_type: 'alarm.created', data: alarm })
    store.applyEvent({
      stream_id: '173-0',
      event_type: 'alarm.updated',
      data: { ...alarm, state: 'ACKNOWLEDGED' },
    })
    expect(store.snapshot.alarms).toHaveLength(1)
    expect(store.snapshot.alarms[0].state).toBe('ACKNOWLEDGED')
  })

  it('does not hardcode R001 when a different robot becomes active', () => {
    const store = useMonitorStore()
    store.applyEvent({
      stream_id: '174-0',
      event_type: 'vehicle.location',
      data: { id: 'robot-2', vehicle_id: 'FIRE-02', x: 7, y: 9 },
    })
    expect(store.activeRobotId).toBe('FIRE-02')
    expect(store.robot?.vehicle_id).toBe('FIRE-02')
  })
})
