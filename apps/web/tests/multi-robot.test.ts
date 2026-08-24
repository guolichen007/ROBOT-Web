import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useMonitorStore } from '@/stores/monitor'
import type { Alarm, MonitorSnapshot, OperationContext, RobotState, StreamInfo, Task } from '@/types'

const mocks = vi.hoisted(() => ({ apiGet: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { get: mocks.apiGet, post: vi.fn() } }))

function makeSnapshot(partial: Partial<MonitorSnapshot>): MonitorSnapshot {
  return {
    snapshot_watermark: '1-0',
    site: null,
    map: null,
    map_version: null,
    parking_slots: [],
    inspection_points: [],
    extinguish_points: [],
    trajectories: [],
    robots: [],
    alarms: [],
    tasks: [],
    streams: [],
    navigation_presets: [],
    ...partial,
  }
}

const robotA: RobotState = { id: 'robot-a', vehicle_id: 'R001', name: 'R001', enabled: true }
const robotB: RobotState = {
  id: 'robot-b',
  vehicle_id: 'firebot-vehicle-01',
  name: 'real',
  enabled: true,
}
const idleContext: OperationContext = {
  state: 'IDLE',
  kind: null,
  task_id: null,
  patrol_plan_id: null,
  last_completed_waypoint_index: null,
  target_waypoint_index: null,
  waypoint_total: null,
  checkpoint_index: null,
  checkpoint_total: null,
  current_slot_code: null,
  next_slot_code: null,
  interrupted_reason: null,
  can_continue: false,
  can_return: true,
}

describe('multi-robot active-vehicle scoping', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    mocks.apiGet.mockReset()
  })

  it('scopes task/alarm/stream/operation-context to the active robot', () => {
    const store = useMonitorStore()
    const taskA = {
      id: 't1',
      robot_id: 'robot-a',
      task_code: 'T1',
      type: 'PATROL',
      status: 'EXECUTING',
      phase: 'INSPECTING',
      progress: 40,
      created_at: '2026-08-21T00:00:00Z',
    } as Task
    const alarmA = {
      id: 'a1',
      robot_id: 'robot-a',
      event_code: 'FE-1',
      state: 'NEW',
      severity: 'HIGH',
      fire_type: 'smoke',
      occurrence_count: 1,
      detection_method: 'AUTO',
      last_seen_at: '2026-08-21T00:00:00Z',
    } as Alarm
    const streamA = {
      stream_id: 'R001-roof',
      id: 's1',
      robot_id: 'robot-a',
      camera_type: 'roof_rgb',
      state: 'LIVE',
    } as StreamInfo
    store.snapshot = makeSnapshot({
      robots: [robotA, robotB],
      tasks: [taskA],
      alarms: [alarmA],
      streams: [streamA],
      operation_contexts: {
        R001: { ...idleContext, state: 'PAUSED', kind: 'PATROL' },
        'firebot-vehicle-01': idleContext,
      },
    })

    store.selectRobot('firebot-vehicle-01')
    expect(store.robot?.vehicle_id).toBe('firebot-vehicle-01')
    expect(store.activeTask).toBeUndefined()
    expect(store.activeRobotAlarms).toHaveLength(0)
    expect(store.activeRobotStreams).toHaveLength(0)
    expect(store.operationContext?.state).toBe('IDLE')

    store.selectRobot('R001')
    expect(store.robot?.vehicle_id).toBe('R001')
    expect(store.activeTask?.id).toBe('t1')
    expect(store.activeRobotAlarms).toHaveLength(1)
    expect(store.activeRobotStreams).toHaveLength(1)
    expect(store.operationContext?.state).toBe('PAUSED')
  })

  it('restores the persisted vehicle and falls back when it is disabled', async () => {
    const store = useMonitorStore()
    localStorage.setItem('firebot.activeVehicleId', 'firebot-vehicle-01')
    mocks.apiGet.mockResolvedValue({ data: makeSnapshot({ robots: [robotA, robotB] }) })
    await store.loadSnapshot()
    expect(store.activeRobotId).toBe('firebot-vehicle-01')
    expect(store.robot?.vehicle_id).toBe('firebot-vehicle-01')

    const disabledB = { ...robotB, enabled: false } as RobotState
    mocks.apiGet.mockResolvedValue({ data: makeSnapshot({ robots: [robotA, disabledB] }) })
    await store.loadSnapshot()
    expect(store.activeRobotId).toBe('R001')
  })

  it('keeps the selected vehicle when it goes offline (no auto-switch)', async () => {
    const store = useMonitorStore()
    localStorage.setItem('firebot.activeVehicleId', 'firebot-vehicle-01')
    mocks.apiGet.mockResolvedValue({ data: makeSnapshot({ robots: [robotA, robotB] }) })
    await store.loadSnapshot()
    const offlineB = { ...robotB, online_state: 'OFFLINE' as const } as RobotState
    mocks.apiGet.mockResolvedValue({ data: makeSnapshot({ robots: [robotA, offlineB] }) })
    await store.loadSnapshot()
    expect(store.activeRobotId).toBe('firebot-vehicle-01')
  })

  it('does not switch the active vehicle when another vehicle emits events', () => {
    const store = useMonitorStore()
    store.snapshot = makeSnapshot({ robots: [robotA, robotB] })
    store.selectRobot('firebot-vehicle-01')
    store.applyEvent({
      stream_id: '9-0',
      event_type: 'vehicle.location',
      data: { vehicle_id: 'R001', x: 9, y: 9 },
    })
    expect(store.activeRobotId).toBe('firebot-vehicle-01')
    expect(store.snapshot.robots.find((r) => r.vehicle_id === 'R001')?.x).toBe(9)
  })

  it('refuses to select a disabled robot', () => {
    const store = useMonitorStore()
    const disabledB = { ...robotB, enabled: false } as RobotState
    store.snapshot = makeSnapshot({ robots: [robotA, disabledB] })
    expect(store.selectRobot('firebot-vehicle-01')).toBe(false)
    expect(store.activeRobotId).toBeNull()
  })

  it('falls back immediately when the active vehicle is disabled by another client', () => {
    const store = useMonitorStore()
    store.snapshot = makeSnapshot({ robots: [robotA, robotB] })
    store.selectRobot('firebot-vehicle-01')
    expect(store.activeRobotId).toBe('firebot-vehicle-01')

    store.applyEvent({
      stream_id: '10-0',
      event_type: 'robot.updated',
      data: { vehicle_id: 'firebot-vehicle-01', enabled: false },
    })
    expect(store.activeRobotId).toBe('R001')
    expect(localStorage.getItem('firebot.activeVehicleId')).toBe('R001')
  })

  it('does not switch when a non-active vehicle is disabled', () => {
    const store = useMonitorStore()
    store.snapshot = makeSnapshot({ robots: [robotA, robotB] })
    store.selectRobot('firebot-vehicle-01')
    store.applyEvent({
      stream_id: '11-0',
      event_type: 'robot.updated',
      data: { vehicle_id: 'R001', enabled: false },
    })
    expect(store.activeRobotId).toBe('firebot-vehicle-01')
  })
})
