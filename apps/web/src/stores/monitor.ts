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
})

export const useMonitorStore = defineStore('monitor', () => {
  const snapshot = ref<MonitorSnapshot>(emptySnapshot())
  const connected = ref(false)
  const resyncing = ref(false)
  const lastStreamId = ref('0-0')
  let socket: WebSocket | null = null
  let reconnectTimer = 0

  const robot = computed(() => snapshot.value.robots.find((item) => item.vehicle_id === 'R001'))
  const activeAlarm = computed(() => snapshot.value.alarms[0])
  const activeTask = computed(() => snapshot.value.tasks[0])

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
      else snapshot.value.robots.push(event.data as RobotState)
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
      lastStreamId.value = snapshot.value.snapshot_watermark
    } finally {
      resyncing.value = false
    }
  }

  async function connect(): Promise<void> {
    disconnect(false)
    if (!snapshot.value.snapshot_watermark) await loadSnapshot()
    const { data } = await api.post('/auth/ws-ticket')
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    socket = new WebSocket(
      `${protocol}//${location.host}/ws/v1/monitor?ticket=${encodeURIComponent(data.ticket)}&after=${encodeURIComponent(lastStreamId.value)}`,
    )
    socket.onopen = () => {
      connected.value = true
    }
    socket.onmessage = async (message) => {
      const event = JSON.parse(message.data)
      if (event.event_type === 'resync_required') {
        await loadSnapshot()
        await connect()
      } else if (event.stream_id) applyEvent(event)
    }
    socket.onclose = () => {
      connected.value = false
      window.clearTimeout(reconnectTimer)
      reconnectTimer = window.setTimeout(() => void connect(), 1500)
    }
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
    await loadSnapshot()
    await connect()
  }

  return {
    snapshot,
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
  }
})
