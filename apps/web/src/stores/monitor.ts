import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/lib/api'
import type { AgentCommand, Alarm, MonitorSnapshot, RealtimeEvent, RobotState, Task } from '@/types'

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
  // WEB-R1-MIN：服务端撤销会话（账号停用 / 权限变更 / 密码重置）后置 true，停止重连。
  const sessionRevoked = ref(false)
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

  // 命令行“新旧”判定（HTTP 历史/POST 响应与 WS 增量合并用）。服务端生命周期允许
  // PUBLISHED_UNCONFIRMED ↔ ACK_ACCEPTED 双向（迟到 ACK / 状态确认超时），因此非终态不能按
  // 固定秩排序。规则：
  // 1) 已是真正终态的行，永不被非终态覆盖（旧 HTTP 响应不得撤销 WS 已确认终态）；
  // 2) 其余按“服务端最近一次状态变更时间” terminal_at > ack_at > published_at > issued_at 比较；
  // 3) 时间相同则按软件反馈条数，再相同视为同一事实（幂等，不替换）。
  const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED', 'ACK_REJECTED', 'ACK_UNSUPPORTED'])
  function commandStamp(row: AgentCommand): number {
    const value = row.terminal_at || row.ack_at || row.published_at || row.issued_at
    const parsed = value ? Date.parse(value) : NaN
    return Number.isFinite(parsed) ? parsed : 0
  }
  function isNewerCommand(incoming: AgentCommand, current: AgentCommand): boolean {
    const terminalIn = TERMINAL.has(incoming.lifecycle_status)
    const terminalCur = TERMINAL.has(current.lifecycle_status)
    if (terminalCur && !terminalIn) return false
    if (terminalIn && !terminalCur) return true
    const a = commandStamp(incoming)
    const b = commandStamp(current)
    if (a !== b) return a > b
    const feedbackA = incoming.ack_payload_json?.software_feedback?.length ?? 0
    const feedbackB = current.ack_payload_json?.software_feedback?.length ?? 0
    if (feedbackA !== feedbackB) return feedbackA > feedbackB
    // 同一时刻、同反馈数但状态不同（如 ACK_ACCEPTED→EXECUTING 不改时间戳）：
    // EXECUTING 只能由 ACK_ACCEPTED 前进而来
    return incoming.lifecycle_status === 'EXECUTING' && current.lifecycle_status === 'ACK_ACCEPTED'
  }

  /**
   * HTTP 来源（历史查询 / 命令 POST 响应 / 轮询）的命令行合并：不经过 applyEvent，
   * **不推进/改写 WS watermark**（lastStreamId / snapshot_watermark），且只在 incoming 更新时替换，
   * 旧响应不得覆盖 WS 已确认的新终态。返回实际被替换/新增的行数。
   */
  function mergeCommands(rows: AgentCommand[]): number {
    snapshot.value.commands ??= []
    let changed = 0
    for (const row of rows) {
      const list: AgentCommand[] = snapshot.value.commands
      const index = list.findIndex((item) => item.command_id === row.command_id || item.id === row.id)
      if (index < 0) {
        list.push(row)
        changed += 1
      } else if (isNewerCommand(row, list[index]!)) {
        list[index] = { ...list[index], ...row }
        changed += 1
      }
    }
    if (changed) {
      snapshot.value.commands = [...snapshot.value.commands]
        .sort((x, y) => Date.parse(y.issued_at) - Date.parse(x.issued_at))
        .slice(0, 200)
    }
    return changed
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
        const current = snapshot.value.robots[index]
        const incomingChannels = event.data.data_channels
        // data_channels 是部分 channel delta，必须深合并，绝不能整对象覆盖
        // （否则 battery 更新会把 heartbeat/smoke 等其它 channel 全部丢掉）
        snapshot.value.robots[index] = {
          ...current,
          ...event.data,
          ...(incomingChannels
            ? { data_channels: { ...(current.data_channels || {}), ...incomingChannels } }
            : {}),
        }
        // Another client may disable the active vehicle in real time via
        // robot.updated; fall back immediately to the first enabled vehicle.
        // G1-P2-L0 C：只读监测设备（VEHICLE_AGENT_V1）即使被停用仍保留选中；
        // 普通停用设备保持原有回退行为。
        if (event.data.enabled === false) {
          const selected = robot.value
          if (
            selected &&
            (selected.vehicle_id === vehicleId || selected.id === vehicleId) &&
            selected.agent_protocol !== 'VEHICLE_AGENT_V1'
          ) {
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
    } else if (event.event_type === 'command.updated') {
      snapshot.value.commands ??= []
      const incoming = event.data as AgentCommand
      const current = snapshot.value.commands.find((row) => row.id === incoming.id)
      // WS 增量按服务端顺序到达，正常总是更新；仅防御重放/重复终态把已确认终态改回非终态
      if (!current || isNewerCommand(incoming, current) || !isNewerCommand(current, incoming)) {
        replaceById(snapshot.value.commands, incoming)
      }
      snapshot.value.commands = snapshot.value.commands.slice(0, 200)
    }
  }

  async function loadSnapshot(): Promise<void> {
    resyncing.value = true
    try {
      snapshot.value = (await api.get('/monitor/snapshot')).data
      // Restore priority: stored vehicle_id -> in-memory id -> first enabled
      // （只读监测设备 enabled=false+V1 同样可恢复选中）。
      const enabled = enabledRobots.value
      const eligible = snapshot.value.robots.filter(
        (item) => item.enabled !== false || item.agent_protocol === 'VEHICLE_AGENT_V1',
      )
      const stored = readStoredVehicleId()
      let next: string | null = null
      if (stored) {
        const hit = eligible.find((item) => item.vehicle_id === stored)
        if (hit) next = hit.vehicle_id
      }
      if (!next) {
        const current = eligible.find(
          (item) => item.id === activeRobotId.value || item.vehicle_id === activeRobotId.value,
        )
        if (current) next = current.vehicle_id
      }
      if (!next) next = enabled[0]?.vehicle_id || eligible[0]?.vehicle_id || null
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
    // G1-P2-RC1 P0-1：只读监测设备（enabled=false + VEHICLE_AGENT_V1）允许选中；
    // 普通停用设备继续拒绝。
    if (target.enabled === false && target.agent_protocol !== 'VEHICLE_AGENT_V1') return false
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
        if (event.event_type === 'session_revoked') {
          sessionRevoked.value = true
          disconnect(false)
        } else if (event.event_type === 'resync_required') {
          await loadSnapshot()
          await connect()
        } else if (event.stream_id) applyEvent(event)
      }
      socket.onerror = () => socket?.close()
      socket.onclose = (event?: CloseEvent) => {
        connected.value = false
        if (event?.code === 4401) {
          sessionRevoked.value = true
          return
        }
        scheduleReconnect()
      }
    } catch (error: any) {
      connected.value = false
      if (error?.response?.status === 401) {
        sessionRevoked.value = true
        return
      }
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
    sessionRevoked.value = false
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
    sessionRevoked,
    start,
    disconnect,
    loadSnapshot,
    applyEvent,
    mergeCommands,
    reconnectDelay,
  }
})
