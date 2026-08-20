<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ChevronDownIcon, LayersIcon } from 'tdesign-icons-vue-next'
import MapCanvas from '@/components/MapCanvas.vue'
import SituationBanner from '@/components/monitor/SituationBanner.vue'
import MapSelectionBar from '@/components/monitor/MapSelectionBar.vue'
import VideoSurveillancePanel from '@/components/monitor/VideoSurveillancePanel.vue'
import PrimaryAlarmPanel from '@/components/monitor/PrimaryAlarmPanel.vue'
import OperationsCommandDock from '@/components/monitor/OperationsCommandDock.vue'
import DeviceSnapshot from '@/components/monitor/DeviceSnapshot.vue'
import ProgressRingGate4 from '@/components/monitor/ProgressRingGate4.vue'
import { usePrimaryAlarm } from '@/composables/usePrimaryAlarm'
import { useVehicleOperationState } from '@/composables/useVehicleOperationState'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'
import { api, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import { operationalSituation } from '@/lib/operations'
import { alarmStateLabel, alarmTypeLabel, reasonCodeLabel } from '@/lib/ui-labels'
import robotIdleArt from '@/assets/yd/gate4/status/robot_state_idle_art.png'
import robotOnlineArt from '@/assets/yd/gate4/status/robot_state_online_art.png'
import robotAlarmArt from '@/assets/yd/gate4/status/robot_state_alarm_art.png'
import type { AlarmTimelineItem, DetectionCoverage, StopOperation } from '@/types'

const auth = useAuthStore()
const monitor = useMonitorStore()
const selectedSlotId = ref<string | null>(null)
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
  computed(() => monitor.snapshot.alarms),
)
const resumeTaskId = computed(() => {
  const cancelled = monitor.snapshot.tasks.find(
    (item) =>
      item.type === 'PATROL' &&
      item.status === 'CANCELLED' &&
      Boolean(item.parameters_json?.live_route_cursor),
  )
  return cancelled?.id || null
})
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
const { state: vehicleState, atWaitingArea } = useVehicleOperationState({
  robot,
  activeTask,
  stopOperation,
  requestBusy: busyCommand,
  resumeTaskId,
})
const trajectory = computed(() => {
  const trajectories = monitor.snapshot.trajectories
  return (
    trajectories.find((item) => item.code === 'RIGHT_SIDE_S_CRUISE')?.path_json ||
    trajectories[0]?.path_json ||
    []
  )
})
const readOnly = computed(() => robot.value?.integration?.source_kind === 'ROS_COMPAT')
const readinessText = computed(() => {
  const labels = [
    ...new Set((robot.value?.readiness_reasons || []).map((code) => reasonCodeLabel(code) || '控制链路尚未就绪')),
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
const navigationReason = computed(() => {
  if (!selectedSlot.value?.enabled) return '该车位已禁用'
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
const patrolStatus = computed(() =>
  activeTask.value ? '巡检执行中' : patrolOnline.value ? '待命' : '未接入',
)
const patrolProgress = computed(() => activeTask.value?.progress ?? 0)
const patrolCode = computed(() => activeTask.value?.task_code || '—')
const roofStream = computed(() => monitor.snapshot.streams.find((item) => item.camera_type === 'roof_rgb'))
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
  if (vehicleState.value === 'STOPPING' || vehicleState.value === 'STOPPED_RESUMABLE') return 'is-stopping'
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
async function refreshCoverage() {
  if (!robot.value) return
  try {
    coverage.value = (await api.get(`/robots/${robot.value.vehicle_id}/detection-coverage`)).data
  } catch {
    coverage.value = null
  }
}
async function refreshTimeline() {
  timeline.value = primaryAlarm.value ? (await api.get(`/alarms/${primaryAlarm.value.id}/timeline`)).data : []
}
watch(
  () => primaryAlarm.value?.id,
  () => void refreshTimeline(),
  { immediate: true },
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
  if (!robot.value || !selectedPreset.value || navigationReason.value) return
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
  }
}
async function patrol() {
  if (!robot.value) {
    toast('当前无机器人，无法开始巡检')
    return
  }
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
    toast('当前无机器人，无法停止巡检')
    return
  }
  busyCommand.value = 'stop'
  try {
    stopOperation.value = (
      await api.post(
        `/robots/${robot.value.vehicle_id}/stop-patrol`,
        {},
        { headers: { 'Idempotency-Key': newUuid() } },
      )
    ).data
    if (stopOperation.value) void pollStop(stopOperation.value.id)
    toast('正在请求停止巡检，独立确认任务取消与车辆静止')
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
  if (!monitor.connected && !monitor.resyncing) void monitor.start()
  void refreshCoverage()
  coverageTimer = window.setInterval(() => void refreshCoverage(), 1000)
})
onUnmounted(() => {
  window.clearInterval(coverageTimer)
  window.clearInterval(stopTimer)
})
</script>

<template>
  <main class="yd-monitor-view" :class="{ 'is-alarm': Boolean(primaryAlarm) }">
    <div v-if="notice" class="toast">{{ notice }}</div>
    <Teleport to=".workspace-alert">
      <SituationBanner
        :state="situation"
        :alarm="primaryAlarm"
        @select="primaryAlarmId = primaryAlarm?.id || null"
      />
    </Teleport>
    <section class="yd-monitor-core">
      <section class="panel operations-map-panel">
        <header>
          <div>
            <h2>停车场巡检地图</h2>
            <span
              >{{ monitor.snapshot.site?.name || '未配置场站' }} · 地图
              {{ monitor.snapshot.map_version?.version || '--' }}</span
            >
          </div>
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
          :show-route="layers.route"
          :show-coverage="layers.coverage"
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
      <aside class="yd-monitor-side" :class="{ 'is-alarm': Boolean(primaryAlarm) }">
        <VideoSurveillancePanel :streams="monitor.snapshot.streams" />
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
                  >{{ alarm.event_code }}<small
                    >{{ alarmTypeLabel(alarm.fire_type) }} · {{ alarmStateLabel(alarm.state) }}</small
                  ></span
                ><time>{{ new Date(alarm.last_seen_at).toLocaleTimeString('zh-CN', { hour12: false }) }}</time>
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
              <span>{{ activeTask ? `任务进行中 · ${patrolCode}` : '等待下发任务' }}</span>
            </div>
            <div class="yd-patrol-meta">
              <div><span>巡检路线</span><b>右侧全覆盖 S 型</b></div>
              <div v-if="liveCheckpoint"><span>当前巡检</span><b>{{ liveCheckpoint.current_slot_code || '--' }}</b></div>
              <div v-if="liveCheckpoint"><span>下一巡检</span><b>{{ liveCheckpoint.next_slot_code || '--' }}</b></div>
              <div v-if="liveCheckpoint"><span>已巡检</span><b>{{ liveCheckpoint.index }} / {{ liveCheckpoint.total }}</b></div>
              <div v-else><span>任务进度</span><b>{{ patrolProgress }}%</b></div>
            </div>
            <ProgressRingGate4 :value="patrolProgress" label="任务进度" />
          </section>
        </template>
      </aside>
    </section>
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
        <strong>正在停止巡检</strong>
        <span>连续静止帧 {{ stopOperation.stationary_frames }}/5</span>
      </template>
    </section>
    <section class="yd-command-dock">
      <OperationsCommandDock
        :busy="busyCommand"
        :vehicle-state="vehicleState"
        :reason="dockReason"
        :estop-active="Boolean(robot?.estop_active)"
        :at-waiting-area="atWaitingArea"
        @patrol="patrol"
        @stop="stop"
        @home="home"
        @estop="estop"
        @reset-estop="resetEstop"
      />
    </section>
  </main>
</template>
