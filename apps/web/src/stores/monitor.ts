import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/lib/api'
import type { Alarm, MonitorSnapshot, RealtimeEvent, RobotState, Task } from '@/types'

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

  const robot = computed(() =>
    snapshot.value.robots.find(
      (item) => item.id === activeRobotId.value || item.vehicle_id === activeRobotId.value,
    ),
  )
  const activeAlarm = computed(() => snapshot.value.alarms[0])
  const activeTask = computed(() =>
    snapshot.value.tasks.find((item) => ['CREATED', 'QUEUED', 'ACCEPTED', 'EXECUTING'].includes(item.status)),
  )

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
      if (index >= 0) snapshot.value.robots[index] = { ...snapshot.value.robots[index], ...event.data }
      else {
        snapshot.value.robots.push(event.data as RobotState)
        if (!activeRobotId.value) activeRobotId.value = vehicleId
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
      const ids = [...snapshot.value.robots]
        .filter((item) => item.enabled !== false)
        .sort((left, right) => left.vehicle_id.localeCompare(right.vehicle_id))
        .map((item) => item.id || item.vehicle_id)
      if (!activeRobotId.value || !ids.includes(activeRobotId.value)) activeRobotId.value = ids[0] || null
      lastStreamId.value = snapshot.value.snapshot_watermark
    } finally {
      resyncing.value = false
    }
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
    activeAlarm,
    activeTask,
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
