import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { useMonitorStore } from '@/stores/monitor'
import type { DataChannel, MonitorSnapshot, RobotState } from '@/types'

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

describe('data_channels partial deep merge', () => {
  beforeEach(() => setActivePinia(createPinia()))

  function makeSnapshot(robots: RobotState[]): MonitorSnapshot {
    return {
      snapshot_watermark: '1-0',
      site: null,
      map: null,
      map_version: null,
      parking_slots: [],
      inspection_points: [],
      extinguish_points: [],
      trajectories: [],
      robots,
      alarms: [],
      tasks: [],
      streams: [],
      navigation_presets: [],
    }
  }

  function ch(channel: string, last: string): DataChannel {
    return {
      channel,
      support_state: 'CONNECTED',
      quality: 'GOOD',
      source_kind: 'CANONICAL_MQTT',
      last_received_at: last,
    }
  }

  it('battery delta deep-merges and preserves smoke/heartbeat', () => {
    const store = useMonitorStore()
    store.snapshot = makeSnapshot([
      {
        id: 'r1',
        vehicle_id: 'firebot-vehicle-02',
        enabled: true,
        data_channels: {
          heartbeat: ch('heartbeat', 't0'),
          smoke: ch('smoke', 't0'),
          battery: ch('battery', 't0'),
        },
      },
    ])
    store.applyEvent({
      stream_id: '200-0',
      event_type: 'vehicle.status',
      data: {
        vehicle_id: 'firebot-vehicle-02',
        battery: 63.1,
        data_channels: { battery: ch('battery', 't1') },
      },
    })
    const dc = store.snapshot.robots[0].data_channels
    expect(dc?.battery?.last_received_at).toBe('t1')
    expect(dc?.smoke?.last_received_at).toBe('t0')
    expect(dc?.heartbeat?.last_received_at).toBe('t0')
  })

  it('first battery channel is created when snapshot lacked it', () => {
    const store = useMonitorStore()
    store.snapshot = makeSnapshot([
      {
        id: 'r1',
        vehicle_id: 'firebot-vehicle-02',
        enabled: true,
        data_channels: { heartbeat: ch('heartbeat', 't0') },
      },
    ])
    store.applyEvent({
      stream_id: '201-0',
      event_type: 'vehicle.status',
      data: {
        vehicle_id: 'firebot-vehicle-02',
        battery: 63.1,
        data_channels: { battery: ch('battery', 't1') },
      },
    })
    const dc = store.snapshot.robots[0].data_channels
    expect(dc?.battery?.last_received_at).toBe('t1')
    expect(dc?.heartbeat?.last_received_at).toBe('t0')
  })

  it('smoke delta does not drop battery', () => {
    const store = useMonitorStore()
    store.snapshot = makeSnapshot([
      {
        id: 'r1',
        vehicle_id: 'firebot-vehicle-02',
        enabled: true,
        data_channels: { battery: ch('battery', 't0') },
      },
    ])
    store.applyEvent({
      stream_id: '202-0',
      event_type: 'vehicle.sensor',
      data: {
        vehicle_id: 'firebot-vehicle-02',
        smoke: 0.345,
        data_channels: { smoke: ch('smoke', 't1') },
      },
    })
    const dc = store.snapshot.robots[0].data_channels
    expect(dc?.smoke?.last_received_at).toBe('t1')
    expect(dc?.battery?.last_received_at).toBe('t0')
  })
})
