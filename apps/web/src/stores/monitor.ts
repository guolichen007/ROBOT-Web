import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/lib/api'
import type { Alarm, MonitorSnapshot, RealtimeEvent, RobotState, Task } from '@/types'

const ACTIVE_VEHICLE_KEY = 'firebot.activeVehicleId'
const ACTIVE_TASK_STATUSES = ['CREATED', 'QUEUED', 'ACCEPTED', 'EXECUTING']

const emptySnapshot = (): MonitorSnapshot => ({
  snapshot_watermark: '0-0',
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
})

function readStoredVehicleId(): string | null {
  try {
    return localStorage.getItem(ACTIVE_VEHICLE_KEY)
  } catch {
    return null
  }
}

function writeStoredVehicleId(vehicleId: string | null): void {
  try {
    if (vehicleId) localStorage.setItem(ACTIVE_VEHICLE_KEY, vehicleId)
    else localStorage.removeItem(ACTIVE_VEHICLE_KEY)
  } catch {
    /* storage unavailable; selection remains in-memory only */
  }
}

export const useMonitorStore = defineStore('monitor', () => {
  const snapshot = ref<MonitorSnapshot>(emptySnapshot())
  const activeRobotId = ref<string | null>(null)
  const connected = ref(false)
  const resyncing = ref(false)
  const lastStreamId = ref('0-0')
  let socket: WebSocket | null = null
  let reconnectTimer = 0
  let reconnectAttempt = 0
  let connecting = false
  let starting: Promise<void> | null = null

  const enabledRobots = computed(() =>
    [...snapshot.value.robots]
      .filter((item) => item.enabled !== false)
      .sort((left, right) => left.vehicle_id.localeCompare(right.vehicle_id)),
  )

  const robot = computed(() =>
    snapshot.value.robots.find(
      (item) => item.id === activeRobotId.value || item.vehicle_id === activeRobotId.value,
    ),
  )

  // ---------- ACTIVE-VEHICLE SCOPED SELECTORS ----------
  // All Monitor-side UI must consume these; never scan the global lists again.
  const activeRobotTasks = computed(() => {
    const selected = robot.value
    if (!selected) return []
    return snapshot.value.tasks.filter((item) => item.robot_id === selected.id)
  })

  const activeTask = computed(() =>
    activeRobotTasks.value.find((item) => ACTIVE_TASK_STATUSES.includes(item.status)),
  )

  const activeRobotAlarms = computed(() => {
    const selected = robot.value
    if (!selected) return []
    return snapshot.value.alarms.filter((item) => item.robot_id === selected.id)
  })

  const activeRobotStreams = computed(() => {
    const selected = robot.value
    if (!selected) return []
    return snapshot.value.streams.filter((item) => item.robot_id === selected.id)
  })

  const operationContext = computed(() => {
    const key = robot.value?.vehicle_id
    return (key && snapshot.value.operation_contexts?.[key]) || null
  })

  function activeTaskOf(robotId: string): Task | undefined {
    return snapshot.value.tasks.find(
      (item) => item.robot_id === robotId && ACTIVE_TASK_STATUSES.includes(item.status),
    )
  }

  // Kept as a scoped alias for any legacy consumer.
  const activeAlarm = computed(() => activeRobotAlarms.value[0])

  function replaceById<T extends { id?: string }>(rows: T[], value: T): void {
    const index = rows.findIndex((row) => row.id === value.id)
    if (index >= 0) rows[index] = { ...rows[index], ...value }
    else rows.unshift(value)
  }

  function applyEvent(event: RealtimeEvent): void {
    lastStreamId.value = event.stream_id
    snapshot.value.snapshot_watermark = event.stream_id
    if (event.event_type.startsWith('robot.') || event.event_type.startsWith('vehicle.')) {
      const vehicleId = String(event.data.vehicle_id || '')
      const index = snapshot.value.robots.findIndex((item) => item.vehicle_id === vehicleId)
      // Events for OTHER vehicles update their cache but must never switch the
      // active vehicle away from what the operator is looking at.
      if (index >= 0) {
        snapshot.value.robots[index] = { ...snapshot.value.robots[index], ...event.data }
        // Another client may disable the active vehicle in real time via
        // robot.updated; fall back immediately to the first enabled vehicle.
        if (event.data.enabled === false) {
          const selected = robot.value
          if (selected && (selected.vehicle_id === vehicleId || selected.id === vehicleId)) {
            selectFirstEnabledVehicle()
          }
        }
      } else {
        snapshot.value.robots.push(event.data as RobotState)
        // Only initialize a selection when nothing is selected yet; never switch.
        if (!activeRobotId.value && event.data.enabled !== false) activeRobotId.value = vehicleId
      }
    } else if (event.event_type.startsWith('alarm.')) {
      replaceById(snapshot.value.alarms, event.data as Alarm)
    } else if (event.event_type.startsWith('task.')) {
      replaceById(snapshot.value.tasks, event.data as Task)
    }
  }

  async function loadSnapshot(): Promise<void> {
    resyncing.value = true
    try {
      snapshot.value = (await api.get('/monitor/snapshot')).data
      // Restore priority: stored vehicle_id -> in-memory id -> first enabled.
      const enabled = enabledRobots.value
      const stored = readStoredVehicleId()
      let next: string | null = null
      if (stored) {
        const hit = enabled.find((item) => item.vehicle_id === stored)
        if (hit) next = hit.vehicle_id
      }
      if (!next) {
        const current = enabled.find(
          (item) => item.id === activeRobotId.value || item.vehicle_id === activeRobotId.value,
        )
        if (current) next = current.vehicle_id
      }
      if (!next) next = enabled[0]?.vehicle_id || null
      activeRobotId.value = next
      if (next !== stored) writeStoredVehicleId(next)
      lastStreamId.value = snapshot.value.snapshot_watermark
    } finally {
      resyncing.value = false
    }
  }

  function selectRobot(vehicleId: string): boolean {
    const target = snapshot.value.robots.find(
      (item) => item.vehicle_id === vehicleId || item.id === vehicleId,
    )
    if (!target) return false
    if (target.enabled === false) return false
    activeRobotId.value = target.vehicle_id
    writeStoredVehicleId(target.vehicle_id)
    return true
  }

  function selectFirstEnabledVehicle(): void {
    const next = enabledRobots.value[0]?.vehicle_id ?? null
    activeRobotId.value = next
    writeStoredVehicleId(next)
  }

  async function connect(): Promise<void> {
    if (connecting) return
    connecting = true
    disconnect(false)
    try {
      if (!snapshot.value.snapshot_watermark) await loadSnapshot()
      const { data } = await api.post('/auth/ws-ticket')
      const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(
        `${protocol}//${location.host}/ws/v1/monitor?ticket=${encodeURIComponent(data.ticket)}&after=${encodeURIComponent(lastStreamId.value)}`,
      )
      socket.onopen = () => {
        connected.value = true
        reconnectAttempt = 0
      }
      socket.onmessage = async (message) => {
        const event = JSON.parse(message.data)
        if (event.event_type === 'resync_required') {
          await loadSnapshot()
          await connect()
        } else if (event.stream_id) applyEvent(event)
      }
      socket.onerror = () => socket?.close()
      socket.onclose = () => {
        connected.value = false
        scheduleReconnect()
      }
    } catch {
      connected.value = false
      scheduleReconnect()
    } finally {
      connecting = false
    }
  }

  function reconnectDelay(attempt: number, random = Math.random()): number {
    const exponential = Math.min(30_000, 500 * 2 ** Math.min(attempt, 6))
    return Math.round(exponential * (0.8 + random * 0.4))
  }

  function scheduleReconnect(): void {
    window.clearTimeout(reconnectTimer)
    const delay = reconnectDelay(reconnectAttempt++)
    reconnectTimer = window.setTimeout(() => void connect(), delay)
  }

  function disconnect(reconnect = false): void {
    window.clearTimeout(reconnectTimer)
    if (socket) {
      socket.onclose = null
      socket.close()
      socket = null
    }
    connected.value = false
    if (reconnect) reconnectTimer = window.setTimeout(() => void connect(), 1500)
  }

  async function start(): Promise<void> {
    if (!starting) {
      starting = (async () => {
        await loadSnapshot()
        await connect()
      })().finally(() => {
        starting = null
      })
    }
    await starting
  }

  return {
    snapshot,
    activeRobotId,
    robot,
    enabledRobots,
    activeAlarm,
    activeRobotTasks,
    activeTask,
    activeRobotAlarms,
    activeRobotStreams,
    operationContext,
    activeTaskOf,
    selectRobot,
    connected,
    resyncing,
    lastStreamId,
    start,
    disconnect,
    loadSnapshot,
    applyEvent,
    reconnectDelay,
  }
})
