<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ChevronDownIcon, LayersIcon } from 'tdesign-icons-vue-next'
import MapCanvas from '@/components/MapCanvas.vue'
import SituationBanner from '@/components/monitor/SituationBanner.vue'
import MapSelectionBar from '@/components/monitor/MapSelectionBar.vue'
import VideoSurveillancePanel from '@/components/monitor/VideoSurveillancePanel.vue'
import VehicleAgentControls from '@/components/monitor/VehicleAgentControls.vue'
import VehicleDiagnosticsPanel from '@/components/monitor/VehicleDiagnosticsPanel.vue'
import DeviceControlPanel from '@/components/monitor/DeviceControlPanel.vue'
import VehicleAgentKpis from '@/components/monitor/VehicleAgentKpis.vue'
import PrimaryAlarmPanel from '@/components/monitor/PrimaryAlarmPanel.vue'
import OperationsCommandDock from '@/components/monitor/OperationsCommandDock.vue'
import ConsoleStatusBar from '@/components/monitor/ConsoleStatusBar.vue'
import { useOperationReadiness } from '@/composables/useOperationReadiness'
import { useAgentCommands } from '@/composables/useAgentCommands'
import ManualControl from '@/components/ManualControl.vue'
import DeviceSnapshot from '@/components/monitor/DeviceSnapshot.vue'
import ProgressRingGate4 from '@/components/monitor/ProgressRingGate4.vue'
import { usePrimaryAlarm } from '@/composables/usePrimaryAlarm'
import { useVehicleOperationState } from '@/composables/useVehicleOperationState'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'
import { api, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import { pollBackoff } from '@/lib/vehicle-diagnostics'
import { operationalSituation } from '@/lib/operations'
import { alarmStateLabel, alarmTypeLabel, reasonCodeLabel } from '@/lib/ui-labels'
import robotIdleArt from '@/assets/yd/gate4/status/robot_state_idle_art.png'
import robotOnlineArt from '@/assets/yd/gate4/status/robot_state_online_art.png'
import robotAlarmArt from '@/assets/yd/gate4/status/robot_state_alarm_art.png'
import type { AlarmTimelineItem, DetectionCoverage, StopOperation } from '@/types'

const auth = useAuthStore()
const monitor = useMonitorStore()
const selectedSlotId = ref<string | null>(null)
// WEB-R1C 左侧车辆控制面板页签（设备 / 手动 / 监测）
const controlTab = ref<'device' | 'manual' | 'monitor'>('device')
const CONTROL_TABS = [
  { id: 'device', label: '设备' },
  { id: 'manual', label: '手动' },
  { id: 'monitor', label: '监测' },
] as const
const mapMode = ref('live')
const busyMode = ref('')
const busyCommand = ref('')
const timeline = ref<AlarmTimelineItem[]>([])
const coverage = ref<DetectionCoverage | null>(null)
const stopOperation = ref<StopOperation | null>(null)
const notice = ref('')
const layerMenuOpen = ref(false)
const layers = reactive({ route: true, coverage: true, semantic: false })
const otherEventsOpen = ref(false)
const { activeAlarms, primaryAlarm, primaryAlarmId } = usePrimaryAlarm(
  computed(() => monitor.activeRobotAlarms),
)
const operationContext = computed(() => monitor.operationContext)
const resumeTaskId = computed(() => {
  const context = operationContext.value
  return context && context.kind === 'PATROL' && context.can_continue ? context.task_id : null
})
const interruptedKind = computed<'patrol' | 'return' | null>(() => {
  const context = operationContext.value
  if (!context || !['PAUSED', 'ESTOPPED'].includes(context.state)) return null
  return context.kind === 'RETURN' ? 'return' : context.kind === 'PATROL' ? 'patrol' : null
})
const resumeNextSlot = computed(() => operationContext.value?.next_slot_code || undefined)
let coverageTimer = 0
let stopTimer = 0

const robot = computed(() => monitor.robot)
const selectedSlot = computed(() =>
  monitor.snapshot.parking_slots.find((item) => item.id === selectedSlotId.value),
)
const selectedPreset = computed(() =>
  monitor.snapshot.navigation_presets?.find(
    (item) => item.category === 'INSPECTION' && item.parking_slot_id === selectedSlotId.value,
  ),
)
const activeTask = computed(() => monitor.activeTask)
const {
  state: vehicleState,
  atWaitingArea,
  resumeOptions,
} = useVehicleOperationState({
  robot,
  activeTask,
  stopOperation,
  requestBusy: busyCommand,
  resumeTaskId,
  interruptedKind,
})
const trajectory = computed(() => {
  const trajectories = monitor.snapshot.trajectories
  return (
    trajectories.find((item) => item.code === 'RIGHT_SIDE_S_CRUISE')?.path_json ||
    trajectories[0]?.path_json ||
    []
  )
})
const readOnly = computed(
  () => robot.value?.integration?.source_kind === 'ROS_COMPAT' || robot.value?.enabled === false,
)
// WEB-R1B: OperationReadiness is the single authority (legacy keys only when the server lacks it;
// a stale/unavailable answer disables everything).
const ops = useOperationReadiness(robot)
const opsReadiness = ops.readiness
const opsAuthority = ops.authority
const patrolReady = computed(() =>
  ops.allowed('patrol_start', robot.value?.autonomous_task_ready?.patrol === true),
)
const returnReady = computed(() =>
  ops.allowed('return_home', robot.value?.autonomous_task_ready?.return_dock === true),
)
const stopReady = computed(() =>
  ops.allowed('stop_motion', robot.value?.safety_command_ready?.stop_motion === true),
)
const estopReady = computed(() =>
  ops.allowed('emergency_stop', robot.value?.safety_command_ready?.emergency_stop === true),
)
const resetEstopReady = computed(() =>
  ops.allowed('reset_estop', robot.value?.safety_command_ready?.reset_estop === true),
)
// WEB-R1C: ONE vehicle_agent v1 command tracker shared by the device tab and the task bar.
const parkingSlots = computed(() => monitor.snapshot.parking_slots)
const agent = useAgentCommands(selectedSlotId, parkingSlots)
const agentBound = computed(() => robot.value?.agent_protocol === 'VEHICLE_AGENT_V1')
// task bar: pause (canonical has no server pause yet → server reason shown) / continue / goal
const pauseState = computed(() => ops.state('patrol_pause'))
const pauseReady = computed(() => pauseState.value.allowed)
const resumeReady = computed(() => patrolReady.value && Boolean(resumeTaskId.value))
const navigateReady = computed(() => !navigationReason.value)
const navigateTarget = computed(() => selectedSlot.value?.code || '')
// visible (not tooltip-only) reasons for the task buttons that are unavailable right now
const taskHint = computed(() => {
  if (agentBound.value) return ''
  const items: string[] = []
  if (!pauseReady.value && vehicleState.value === 'PATROLLING')
    items.push(`暂停：${pauseState.value.labels.join('、')}`)
  if (navigationReason.value) items.push(`目标导航：${navigationReason.value}`)
  return items.join('；')
})
// WEB-1：保护停车只呈现服务端资格（OperationReadiness.actions.protective_stop）。
// 当前服务端尚未提供该动作 → 恒为不可用并显示原因，绝不在前端推导或伪造。
const protectiveStop = computed(() => {
  const entry = ops.readiness.value?.actions?.protective_stop
  if (!entry) return { allowed: false, reason: '服务端尚未提供保护停车能力（未实现）' }
  return {
    allowed: false,
    reason: entry.allowed
      ? '保护停车下发链路尚未接入（待合同冻结）'
      : entry.reasons.map((code) => reasonCodeLabel(code) || code).join('、') || '不可用',
  }
})
const readinessText = computed(() => {
  const labels = [
    ...new Set(
      (robot.value?.readiness_reasons || []).map((code) => reasonCodeLabel(code) || '控制链路尚未就绪'),
    ),
  ].filter(Boolean)
  return labels.join('、')
})
const controlReason = computed(
  () =>
    (readOnly.value ? '当前为只读接入，控制未开放' : '') ||
    readinessText.value ||
    (robot.value?.control_disabled_reason
      ? reasonCodeLabel(robot.value.control_disabled_reason) || '控制链路未验证'
      : '') ||
    '控制链路未验证',
)
const dockReason = computed(() => {
  if (ops.authority.value === 'STALE') return '服务端控制资格未及时更新，全部操作暂不可用'
  const r = ops.readiness.value
  if (ops.authority.value === 'SERVER' && r?.control_frozen) {
    return `控制已冻结：${r.freeze_reasons.map((code) => reasonCodeLabel(code) || code).join('、')}`
  }
  if (readOnly.value) return '当前为只读接入，控制未开放'
  if (readinessText.value) return readinessText.value
  const disabled = robot.value?.control_disabled_reason
  if (disabled) return reasonCodeLabel(disabled) || '控制链路尚未就绪'
  return ''
})
const freshness = computed(() => {
  if (!robot.value?.server_received_at) return '数据离线'
  const age = Math.max(0, (Date.now() - Date.parse(robot.value.server_received_at)) / 1000)
  const stale = robot.value?.integration?.stale_seconds ?? 3
  const offline = robot.value?.integration?.offline_seconds ?? 10
  if (age >= offline) return '数据离线'
  if (age >= stale) return `数据陈旧 ${age.toFixed(1)}s`
  return '数据实时'
})
const situation = computed(() =>
  operationalSituation({
    criticalFire: primaryAlarm.value?.severity === 'CRITICAL',
    websocketConnected: monitor.connected,
    onlineState: robot.value?.online_state,
    localizationStatus: robot.value?.localization_status,
    estopActive: robot.value?.estop_active,
    estopSupport: robot.value?.data_channels?.estop?.support_state || 'NOT_CONNECTED',
  }),
)
// 保持 Teleport host 稳定，避免动态 Teleport 生命周期触发 renderer 异常路径。
const showSituationBanner = computed(() => Boolean(primaryAlarm.value) || situation.value !== 'NORMAL')
const navigationReason = computed(() => {
  if (!selectedSlot.value) return '请先在地图选择目标车位'
  if (!selectedSlot.value.enabled) return '该车位已禁用'
  if (robot.value?.agent_protocol === 'VEHICLE_AGENT_V1') {
    return robot.value.vehicle_agent_ready?.navigate ? '' : '车辆导航或控制合同未就绪'
  }
  if (!selectedPreset.value) return '车位未显式关联 INSPECTION preset'
  if (activeTask.value) return `存在执行中任务：${activeTask.value.type}`
  if (!robot.value?.autonomous_task_ready?.patrol) return controlReason.value
  return ''
})
const extinguishReason = computed(() => {
  if (readOnly.value) return '当前为只读接入，控制未开放'
  if (!robot.value?.autonomous_task_ready?.extinguish) return '控制链路尚未就绪'
  if (activeTask.value) return '当前已有执行中任务'
  return ''
})
const targetSlotId = computed(() => activeTask.value?.target_parking_slot_id)
const primaryAlarmLocation = computed(() => {
  const id = primaryAlarm.value?.parking_slot_id
  if (!id) return undefined
  return monitor.snapshot.parking_slots.find((slot) => slot.id === id)?.code
})

const patrolOnline = computed(() => robot.value?.online_state === 'ONLINE')
// G1-R1：Agent 在线 ≠ 机器人控制就绪。V1 只读设备（未接入底盘）不得显示"待命"。
const v1ReadonlyMonitoring = computed(
  () =>
    robot.value?.agent_protocol === 'VEHICLE_AGENT_V1' &&
    robot.value?.agent_state?.status?.programs?.chassis !== true,
)
const patrolStatus = computed(() => {
  if (activeTask.value) return '巡检执行中'
  if (!patrolOnline.value) return '未接入'
  return v1ReadonlyMonitoring.value ? '只读监测' : '待命'
})
const patrolProgress = computed(() => activeTask.value?.progress ?? 0)
const patrolCode = computed(() => activeTask.value?.task_code || '—')
const roofStream = computed(() => monitor.activeRobotStreams.find((item) => item.camera_type === 'roof_rgb'))
const liveCheckpoint = computed(() => {
  const value = activeTask.value?.parameters_json?.live_checkpoint as
    | { index?: number; total?: number; current_slot_code?: string; next_slot_code?: string }
    | undefined
  return value
})
const patrolArt = computed(() => {
  if (robot.value?.estop_active) return robotAlarmArt
  if (activeTask.value) return robotOnlineArt
  return robotIdleArt
})
const patrolCardClass = computed(() => {
  if (robot.value?.estop_active) return 'is-estop'
  if (vehicleState.value === 'STOPPING' || vehicleState.value === 'PAUSED_SAFE') return 'is-stopping'
  if (vehicleState.value === 'RETURNING' || vehicleState.value === 'RETURN_STARTING') return 'is-returning'
  if (vehicleState.value === 'PATROLLING' || vehicleState.value === 'PATROL_STARTING') return 'is-patrolling'
  return 'is-idle'
})

function toast(value: string) {
  notice.value = value
  window.setTimeout(() => {
    if (notice.value === value) notice.value = ''
  }, 4000)
}
function friendlyError(reason: unknown): string {
  const data = (reason as { response?: { data?: { error?: { code?: string }; detail?: { code?: string } } } })
    ?.response?.data
  const code = data?.error?.code || data?.detail?.code || ''
  const labeled = reasonCodeLabel(code)
  if (labeled) return labeled
  const message = errorMessage(reason)
  const messageLabel = reasonCodeLabel(message)
  if (messageLabel) return messageLabel
  return message
}
// 每个命令用自己的服务器 readiness 做二次防护；reason 只负责展示，不再作为统一真值。
function requireControl(ready: boolean, label: string): boolean {
  if (ready) return true
  toast(dockReason.value || `${label}未就绪`)
  return false
}
async function refreshCoverage(): Promise<boolean> {
  if (!robot.value) return true
  try {
    coverage.value = (await api.get(`/robots/${robot.value.vehicle_id}/detection-coverage`)).data
    return true
  } catch {
    coverage.value = null
    return false
  }
}
// 覆盖率轮询：成功 1s；失败按 2s→4s→…→30s 退避，成功即恢复（API 重建期间不每秒打 502）
let coverageFailures = 0
let coverageStopped = false
function scheduleCoverage(delay: number) {
  window.clearTimeout(coverageTimer)
  if (coverageStopped) return
  coverageTimer = window.setTimeout(async () => {
    const ok = await refreshCoverage()
    if (coverageStopped) return
    coverageFailures = ok ? 0 : coverageFailures + 1
    scheduleCoverage(ok ? 1000 : pollBackoff(coverageFailures))
  }, delay)
}
async function refreshTimeline() {
  timeline.value = primaryAlarm.value ? (await api.get(`/alarms/${primaryAlarm.value.id}/timeline`)).data : []
}
watch(
  () => primaryAlarm.value?.id,
  () => void refreshTimeline(),
  { immediate: true },
)
watch(
  () => robot.value?.vehicle_id,
  () => {
    selectedSlotId.value = null
    stopOperation.value = null
    coverage.value = null
    void refreshCoverage()
  },
)

async function transition(action: 'acknowledge' | 'confirm' | 'resolve') {
  if (!primaryAlarm.value) return
  try {
    await api.post(`/alarms/${primaryAlarm.value.id}/${action}`)
    await monitor.loadSnapshot()
    await refreshTimeline()
  } catch (reason) {
    toast(errorMessage(reason))
  }
}
async function dispatch(mode: string) {
  if (!primaryAlarm.value) return
  if (!robot.value) {
    toast('当前无机器人，无法执行灭火动作')
    return
  }
  if (extinguishReason.value) {
    toast(extinguishReason.value)
    return
  }
  busyMode.value = mode
  try {
    await api.post(
      `/alarms/${primaryAlarm.value.id}/create-task`,
      {
        robot_id: robot.value.vehicle_id,
        trajectory_id: monitor.snapshot.trajectories[0]?.id,
        parameters: { source: 'OPERATIONS_HMI', extinguish_mode: mode },
      },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    await monitor.loadSnapshot()
    await refreshTimeline()
    const label =
      (
        {
          DEPLOY_BLANKET: '灭火帐',
          SPRAY_AGENT: '灭火剂喷射',
          DEPLOY_THEN_SPRAY: '灭火帐+喷射',
        } as Record<string, string>
      )[mode] || '灭火'
    toast(`${label}任务已下发`)
  } catch (reason) {
    toast(friendlyError(reason))
  } finally {
    busyMode.value = ''
  }
}
async function createManualAlarm() {
  if (!selectedSlot.value || !monitor.snapshot.map_version) return
  try {
    const { data } = await api.post(
      '/alarms/manual',
      {
        parking_slot_id: selectedSlot.value.id,
        fire_type: 'unknown',
        note: `地图人工上报：${selectedSlot.value.code}`,
        map_version: monitor.snapshot.map_version.version,
        severity: 'HIGH',
        media: {},
      },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    await monitor.loadSnapshot()
    primaryAlarmId.value = data.id
    toast(`${selectedSlot.value.code} 人工火情已创建`)
  } catch (reason) {
    toast(errorMessage(reason))
  }
}
async function navigate() {
  if (robot.value?.agent_protocol === 'VEHICLE_AGENT_V1') {
    await agent.send('navigate')
    return
  }
  if (!robot.value || !selectedPreset.value || navigationReason.value) return
  busyCommand.value = 'navigate'
  try {
    await api.post(
      `/robots/${robot.value.vehicle_id}/navigate-preset`,
      { navigation_preset_id: selectedPreset.value.id },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    await monitor.loadSnapshot()
    toast(`已创建前往 ${selectedSlot.value?.code} 检测点的任务`)
  } catch (reason) {
    toast(errorMessage(reason))
  } finally {
    busyCommand.value = ''
  }
}
function pause() {
  // canonical patrol has no server pause action yet; the button stays disabled with the server
  // reason, and this handler refuses rather than improvising a pause through another command.
  if (!pauseReady.value) toast(pauseState.value.labels.join('、') || '暂停不可用')
}
async function patrol() {
  if (!robot.value) {
    toast('当前无机器人，无法开始巡检')
    return
  }
  if (!requireControl(patrolReady.value, '巡检')) return
  busyCommand.value = 'patrol'
  try {
    const plans = (await api.get('/patrol-plans')).data as Array<{
      id: string
      robot_id: string
      code: string
      enabled: boolean
    }>
    const enabled = plans.filter((item) => item.robot_id === robot.value?.id && item.enabled)
    const plan =
      enabled.find((item) => item.code === 'RIGHT_SIDE_S_CRUISE_PLAN') ||
      (enabled.length === 1 ? enabled[0] : undefined)
    if (!plan) throw new Error('存在多个可用巡检计划，请前往任务管理选择')
    await api.post(
      '/tasks/patrol-plan',
      {
        robot_id: robot.value.vehicle_id,
        patrol_plan_id: plan.id,
        resume_task_id: resumeTaskId.value || undefined,
        parameters: { source: 'OPERATIONS_HMI' },
      },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    await monitor.loadSnapshot()
    toast(resumeTaskId.value ? '已从上次巡检位置继续' : '巡检任务已创建')
  } catch (reason) {
    toast(friendlyError(reason))
  } finally {
    busyCommand.value = ''
  }
}
async function pollStop(id: string) {
  window.clearInterval(stopTimer)
  stopTimer = window.setInterval(async () => {
    stopOperation.value = (await api.get(`/stop-operations/${id}`)).data
    if (
      ['VEHICLE_STATIONARY_CONFIRMED', 'PARTIAL_UNCONFIRMED', 'UNCONFIRMED', 'FAILED'].includes(
        stopOperation.value?.state || '',
      )
    ) {
      window.clearInterval(stopTimer)
      await monitor.loadSnapshot()
      const state = stopOperation.value?.state
      if (state === 'VEHICLE_STATIONARY_CONFIRMED') {
        window.setTimeout(() => {
          stopOperation.value = null
        }, 2000)
      } else if (state === 'PARTIAL_UNCONFIRMED') {
        window.setTimeout(() => {
          stopOperation.value = null
        }, 3000)
      }
      // UNCONFIRMED / FAILED keeps the bar visible and locks vehicle motion.
    }
  }, 500)
}
async function stop() {
  if (!robot.value) {
    toast('当前无机器人，无法停止')
    return
  }
  if (!requireControl(stopReady.value, '停止')) return
  busyCommand.value = 'stop'
  try {
    stopOperation.value = (
      await api.post(
        `/robots/${robot.value.vehicle_id}/stop-operation`,
        {},
        { headers: { 'Idempotency-Key': newUuid() } },
      )
    ).data
    if (stopOperation.value) void pollStop(stopOperation.value.id)
    toast('正在停止车辆任务，独立确认任务取消与车辆静止')
  } catch (reason) {
    toast(friendlyError(reason))
  } finally {
    busyCommand.value = ''
  }
}
async function home() {
  if (!robot.value) {
    toast('当前无机器人，无法返回待命区')
    return
  }
  if (!requireControl(returnReady.value, '返航')) return
  busyCommand.value = 'home'
  try {
    await api.post(
      '/tasks/return-dock',
      { robot_id: robot.value.vehicle_id, parameters: { source: 'OPERATIONS_HMI' } },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    await monitor.loadSnapshot()
    toast('返回待命区任务已创建')
  } catch (reason) {
    toast(friendlyError(reason))
  } finally {
    busyCommand.value = ''
  }
}
async function estop() {
  if (!robot.value) {
    toast('当前无机器人，无法执行软件急停')
    return
  }
  if (!requireControl(estopReady.value, '软件急停')) return
  busyCommand.value = 'estop'
  try {
    await api.post(
      `/robots/${robot.value.vehicle_id}/commands/emergency-stop`,
      {},
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    await monitor.loadSnapshot()
    toast('软件急停命令已发送，等待车辆确认')
  } catch (reason) {
    toast(friendlyError(reason))
  } finally {
    busyCommand.value = ''
  }
}
async function waitForEstopCleared(commandId?: string): Promise<'cleared' | 'pending' | 'rejected'> {
  const started = Date.now()
  while (Date.now() - started < 10000) {
    try {
      await monitor.loadSnapshot()
    } catch {
      /* keep polling */
    }
    if (robot.value?.estop_active === false) return 'cleared'
    if (commandId) {
      try {
        const command = (await api.get(`/commands/${commandId}`)).data
        if (command?.ack_status === 'rejected') return 'rejected'
      } catch {
        /* command may not exist yet */
      }
    }
    await new Promise((resolve) => window.setTimeout(resolve, 500))
  }
  return robot.value?.estop_active === false ? 'cleared' : 'pending'
}
async function resetEstop() {
  if (!robot.value) {
    toast('当前无机器人，无法解除软件急停')
    return
  }
  if (!requireControl(resetEstopReady.value, '急停复位')) return
  busyCommand.value = 'reset-estop'
  try {
    const { data } = await api.post(
      `/robots/${robot.value.vehicle_id}/commands/reset-estop`,
      {},
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    toast('正在解除软件急停，等待车辆确认')
    const result = await waitForEstopCleared(data?.command_id)
    if (result === 'cleared') toast('软件急停已解除，车辆当前处于待命状态')
    else if (result === 'rejected') toast('复位命令被车辆拒绝')
    else toast('复位命令已下发，但尚未收到车辆状态确认')
  } catch (reason) {
    toast(friendlyError(reason))
  } finally {
    busyCommand.value = ''
  }
}

onMounted(() => {
  scheduleCoverage(0)
})
onUnmounted(() => {
  coverageStopped = true
  window.clearTimeout(coverageTimer)
  window.clearInterval(stopTimer)
})
</script>

<template>
  <main class="yd-monitor-view console-v21" :class="{ 'is-alarm': Boolean(primaryAlarm) }">
    <header class="console-head">
      <ConsoleStatusBar :readiness="opsReadiness" :readiness-authority="opsAuthority" />
      <VehicleAgentKpis />
    </header>
    <div v-if="notice" class="toast">{{ notice }}</div>
    <!-- workspace-alert 与 MonitorView 在同一 App shell mount tick 创建；
         用 Teleport defer 确保 target 在 shell 插入 document 后再解析，避免 target 尚未入 document。 -->
    <Teleport defer to="#workspace-alert">
      <div class="monitor-situation-host">
        <SituationBanner
          v-if="showSituationBanner"
          :state="situation"
          :alarm="primaryAlarm"
          @select="primaryAlarmId = primaryAlarm?.id || null"
        />
      </div>
    </Teleport>
    <section class="yd-monitor-core">
      <section class="panel operations-map-panel">
        <header>
          <div>
            <h2>停车场二维地图</h2>
            <span
              >{{ monitor.snapshot.site?.name || '未配置场站' }} · 地图
              {{ monitor.snapshot.map_version?.version || '--' }}</span
            >
          </div>
          <nav class="hmi-map-tabs" aria-label="地图显示">
            <button type="button" :aria-pressed="mapMode === 'live'" @click="mapMode = 'live'">
              实时地图
            </button>
            <button type="button" :aria-pressed="mapMode === 'parking'" @click="mapMode = 'parking'">
              车位地图
            </button>
          </nav>
          <div class="map-layer-control">
            <button
              class="layer-trigger"
              type="button"
              aria-label="图层"
              @click="layerMenuOpen = !layerMenuOpen"
            >
              <LayersIcon /><span>图层</span><ChevronDownIcon class="layer-chevron" />
            </button>
            <div v-if="layerMenuOpen" class="layer-menu">
              <label><input v-model="layers.route" type="checkbox" /><span>巡检路线</span></label>
              <label><input v-model="layers.coverage" type="checkbox" /><span>检测范围</span></label>
              <label><input v-model="layers.semantic" type="checkbox" /><span>语义点</span></label>
            </div>
          </div>
        </header>
        <MapCanvas
          :map-version="monitor.snapshot.map_version"
          :slots="monitor.snapshot.parking_slots"
          :inspection-points="monitor.snapshot.inspection_points"
          :extinguish-points="monitor.snapshot.extinguish_points"
          :trajectory="trajectory"
          :robot="robot"
          :alarms="activeAlarms"
          :coverage="coverage"
          :selected-slot-id="selectedSlotId || undefined"
          :target-slot-id="targetSlotId"
          :show-semantic-points="layers.semantic"
          :show-route="mapMode === 'live' && layers.route"
          :show-coverage="mapMode === 'live' && layers.coverage"
          @slot-click="selectedSlotId = $event.id"
        />
        <MapSelectionBar
          v-if="selectedSlot"
          :parking-slot="selectedSlot"
          :preset="selectedPreset"
          :robot="robot"
          :coverage="coverage"
          :disabled-reason="navigationReason"
          @cancel="selectedSlotId = null"
          @navigate="navigate"
          @alarm="createManualAlarm"
        />
      </section>
      <div class="console-side">
        <!-- WEB-R1C：右侧仅保留双视频（上 前方监控相机 / 下 红外热成像），纵向、原始比例 -->
        <VideoSurveillancePanel :streams="monitor.activeRobotStreams" />
      </div>
    </section>
    <!-- WEB-R1C：车辆控制面板位于左侧系统设置下方（应用 Shell 插槽，仅综合控制台存在）。
         与 #workspace-alert 相同，用 Teleport defer 等 Shell 插入 document 后再解析 target。 -->
    <Teleport defer to="#sidebar-console-slot">
      <aside class="console-control-panel" aria-label="车辆控制面板" data-testid="console-control-panel">
        <nav class="console-panel-tabs" aria-label="车辆控制页签">
          <button
            v-for="tab in CONTROL_TABS"
            :key="tab.id"
            type="button"
            :aria-selected="controlTab === tab.id"
            :data-tab="tab.id"
            @click="controlTab = tab.id"
          >
            {{ tab.label }}
          </button>
        </nav>
        <div class="console-panel-scroll">
          <section v-show="controlTab === 'device'" class="console-panel-section">
            <DeviceControlPanel
              v-model:selected-slot-id="selectedSlotId"
              :protocol-bound="agentBound"
              :commands="agent"
              :slots="monitor.snapshot.parking_slots"
              :action-state="ops.state"
            />
            <VehicleDiagnosticsPanel />
          </section>
          <section v-show="controlTab === 'monitor'" class="console-panel-section hmi-monitor-details">
            <template v-if="primaryAlarm">
              <PrimaryAlarmPanel
                :alarm="primaryAlarm"
                :timeline="timeline"
                :disabled-reason="extinguishReason"
                :busy-mode="busyMode"
                :location-label="primaryAlarmLocation"
                :permissions="{
                  ack: auth.can('alarm.ack'),
                  confirm: auth.can('alarm.confirm'),
                  resolve: auth.can('alarm.resolve'),
                }"
                @transition="transition"
                @execute="dispatch"
              />
              <section class="panel secondary-alarms" :class="{ open: otherEventsOpen }">
                <header>
                  <button
                    class="other-events-toggle"
                    type="button"
                    @click="otherEventsOpen = !otherEventsOpen"
                  >
                    <strong>其他事件 ({{ Math.max(0, activeAlarms.length - 1) }})</strong>
                    <span v-if="!otherEventsOpen && activeAlarms.length > 1" class="other-events-summary">
                      {{ activeAlarms[1]?.event_code }} · {{ alarmTypeLabel(activeAlarms[1]?.fire_type) }}
                    </span>
                    <ChevronDownIcon class="other-events-chevron" :class="{ open: otherEventsOpen }" />
                  </button>
                </header>
                <div v-if="otherEventsOpen" class="other-events-list">
                  <button
                    v-for="alarm in activeAlarms.filter((item) => item.id !== primaryAlarmId)"
                    :key="alarm.id"
                    :class="{ active: primaryAlarmId === alarm.id }"
                    @click="primaryAlarmId = alarm.id"
                  >
                    <i :data-level="alarm.severity"></i
                    ><span
                      >{{ alarm.event_code
                      }}<small
                        >{{ alarmTypeLabel(alarm.fire_type) }} · {{ alarmStateLabel(alarm.state) }}</small
                      ></span
                    ><time>{{
                      new Date(alarm.last_seen_at).toLocaleTimeString('zh-CN', { hour12: false })
                    }}</time>
                  </button>
                  <div v-if="activeAlarms.length <= 1" class="quiet-state">暂无其他活动事件</div>
                </div>
              </section>
            </template>
            <template v-else>
              <DeviceSnapshot :robot="robot" :task="activeTask" :stream="roofStream" :freshness="freshness" />
              <section class="panel yd-patrol-card" :class="patrolCardClass">
                <img class="patrol-art" :src="patrolArt" alt="" aria-hidden="true" />
                <div class="patrol-state">
                  <span class="ptitle">巡检状态</span>
                  <strong>{{ patrolStatus }}</strong>
                  <span>{{
                    vehicleState === 'PAUSED_SAFE'
                      ? resumeNextSlot
                        ? `已停止，可从 ${resumeNextSlot} 继续`
                        : '已停止'
                      : activeTask
                        ? `任务进行中 · ${patrolCode}`
                        : '等待下发任务'
                  }}</span>
                </div>
                <div class="yd-patrol-meta">
                  <div><span>巡检路线</span><b>右侧全覆盖 S 型</b></div>
                  <div v-if="liveCheckpoint">
                    <span>当前巡检</span><b>{{ liveCheckpoint.current_slot_code || '--' }}</b>
                  </div>
                  <div v-if="liveCheckpoint">
                    <span>下一巡检</span><b>{{ liveCheckpoint.next_slot_code || '--' }}</b>
                  </div>
                  <div v-if="liveCheckpoint">
                    <span>已巡检</span><b>{{ liveCheckpoint.index }} / {{ liveCheckpoint.total }}</b>
                  </div>
                  <div v-else>
                    <span>任务进度</span><b>{{ patrolProgress }}%</b>
                  </div>
                </div>
                <ProgressRingGate4 :value="patrolProgress" label="任务进度" />
              </section>
            </template>
          </section>
          <section v-show="controlTab === 'manual'" class="console-panel-section hmi-manual-controls">
            <p class="hmi-hint">
              人工控制须服务端判定就绪且持有独占控制权；不满足时按钮不可用，不会申请租约或发送控制帧。
            </p>
            <ManualControl
              :robot="robot"
              :show-safety="false"
              @notice="(message: string) => toast(message)"
            />
          </section>
        </div>
      </aside>
    </Teleport>
    <footer class="console-action-bar" aria-label="固定任务操作栏" data-testid="console-action-bar">
      <OperationsCommandDock
        :busy="busyCommand"
        :vehicle-state="vehicleState"
        :reason="dockReason"
        :estop-active="Boolean(robot?.estop_active)"
        :at-waiting-area="atWaitingArea"
        :resume-options="resumeOptions"
        :patrol-ready="patrolReady"
        :return-ready="returnReady"
        :stop-ready="stopReady"
        :estop-ready="estopReady"
        :reset-estop-ready="resetEstopReady"
        :pause-ready="pauseReady"
        :pause-reason="pauseState.labels.join('、')"
        :resume-ready="resumeReady"
        :navigate-ready="navigateReady"
        :navigate-target="navigateTarget"
        :navigate-reason="navigationReason"
        :hint="taskHint"
        @patrol="patrol"
        @pause="pause"
        @resume="patrol"
        @navigate="navigate"
        @stop="stop"
        @home="home"
        @estop="estop"
        @reset-estop="resetEstop"
      >
        <template v-if="agentBound" #task>
          <VehicleAgentControls
            v-model:selected-slot-id="selectedSlotId"
            variant="bar"
            :commands="agent"
            :actions="['patrol', 'stop_patrol', 'resume_patrol', 'navigate', 'stop_navigate', 'homing']"
            :slots="monitor.snapshot.parking_slots"
          />
        </template>
        <template #safety-extra>
          <div class="console-protective" data-testid="protective-stop">
            <button
              type="button"
              class="protective-stop"
              :disabled="!protectiveStop.allowed"
              :aria-describedby="'protective-reason'"
            >
              保护停车
            </button>
            <small id="protective-reason">{{ protectiveStop.reason }}</small>
          </div>
        </template>
      </OperationsCommandDock>
    </footer>
    <section
      v-if="stopOperation"
      class="stop-operation-state"
      :class="{
        'stop-success': stopOperation.state === 'VEHICLE_STATIONARY_CONFIRMED',
        'stop-warning': stopOperation.state === 'PARTIAL_UNCONFIRMED',
        'stop-danger': ['UNCONFIRMED', 'FAILED'].includes(stopOperation.state),
      }"
      aria-live="polite"
    >
      <template v-if="stopOperation.state === 'VEHICLE_STATIONARY_CONFIRMED'">
        <strong>车辆已停止</strong>
        <span>任务已取消，车辆静止已确认</span>
      </template>
      <template v-else-if="stopOperation.state === 'PARTIAL_UNCONFIRMED'">
        <strong>车辆已静止，但部分回执未确认</strong>
        <span>连续静止帧 {{ stopOperation.stationary_frames }}/5</span>
      </template>
      <template v-else-if="['UNCONFIRMED', 'FAILED'].includes(stopOperation.state)">
        <strong>无法确认车辆已经停止</strong>
        <span>禁止继续巡检与返航</span>
      </template>
      <template v-else>
        <strong>正在停止车辆任务</strong>
        <span>连续静止帧 {{ stopOperation.stationary_frames }}/5</span>
      </template>
    </section>
  </main>
</template>
