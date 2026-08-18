<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import MapCanvas from '@/components/MapCanvas.vue'
import ManualControl from '@/components/ManualControl.vue'
import SituationBanner from '@/components/monitor/SituationBanner.vue'
import RiskTelemetryRibbon from '@/components/monitor/RiskTelemetryRibbon.vue'
import MapSelectionBar from '@/components/monitor/MapSelectionBar.vue'
import VideoSurveillancePanel from '@/components/monitor/VideoSurveillancePanel.vue'
import PrimaryAlarmPanel from '@/components/monitor/PrimaryAlarmPanel.vue'
import OperationsCommandDock from '@/components/monitor/OperationsCommandDock.vue'
import { usePrimaryAlarm } from '@/composables/usePrimaryAlarm'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'
import { api, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import { operationalSituation } from '@/lib/operations'
import type { AlarmTimelineItem, DetectionCoverage, StopOperation } from '@/types'

const auth = useAuthStore()
const monitor = useMonitorStore()
const selectedSlotId = ref<string | null>(null)
const extinguishMode = ref('DEPLOY_THEN_SPRAY')
const timeline = ref<AlarmTimelineItem[]>([])
const coverage = ref<DetectionCoverage | null>(null)
const stopOperation = ref<StopOperation | null>(null)
const notice = ref('')
const working = ref(false)
const manualOpen = ref(false)
const { activeAlarms, primaryAlarm, primaryAlarmId } = usePrimaryAlarm(
  computed(() => monitor.snapshot.alarms),
)
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
const trajectory = computed(() => monitor.snapshot.trajectories[0]?.path_json || [])
const roofStream = computed(() => monitor.snapshot.streams.find((item) => item.camera_type === 'roof_rgb'))
const stopReady = computed(() => Boolean(robot.value?.safety_command_ready?.stop_motion))
const estopReady = computed(() => Boolean(robot.value?.safety_command_ready?.emergency_stop))
const autonomousReady = computed(() => Boolean(robot.value?.autonomous_task_ready?.patrol))
const manualReady = computed(
  () =>
    Boolean(robot.value?.manual_control_ready) &&
    Boolean(robot.value?.supported_commands?.includes('manual_control')) &&
    !readOnly.value,
)
const readOnly = computed(() => robot.value?.integration?.source_kind === 'ROS_COMPAT')
const controlReason = computed(
  () =>
    (readOnly.value ? 'ROS1 只读接入中，尚未验证下行控制链路' : '') ||
    robot.value?.readiness_reasons?.join('、') ||
    robot.value?.control_disabled_reason ||
    '控制链路未验证',
)
const freshness = computed(() => {
  if (!robot.value?.server_received_at) return '无数据'
  return `${Math.max(0, (Date.now() - Date.parse(robot.value.server_received_at)) / 1000).toFixed(1)} 秒前`
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
  if (readOnly.value) return '只读接入中，尚未验证灭火执行链路'
  if (!robot.value?.autonomous_task_ready?.extinguish) return controlReason.value
  if (activeTask.value) return `存在执行中任务：${activeTask.value.type}`
  return ''
})
const targetSlotId = computed(() => activeTask.value?.target_parking_slot_id)

const patrolOnline = computed(() => robot.value?.online_state === 'ONLINE')
const patrolStatus = computed(() =>
  activeTask.value ? '巡检执行中' : patrolOnline.value ? '待命' : '未接入',
)
const patrolMode = computed(() => activeTask.value?.type || '—')
const patrolProgress = computed(() => activeTask.value?.progress ?? 0)
const patrolCode = computed(() => activeTask.value?.task_code || '—')

function toast(value: string) {
  notice.value = value
  window.setTimeout(() => {
    if (notice.value === value) notice.value = ''
  }, 4000)
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
async function dispatch() {
  if (!primaryAlarm.value || !robot.value) return
  working.value = true
  try {
    await api.post(
      `/alarms/${primaryAlarm.value.id}/create-task`,
      {
        robot_id: robot.value.vehicle_id,
        trajectory_id: monitor.snapshot.trajectories[0]?.id,
        parameters: { source: 'OPERATIONS_HMI', extinguish_mode: extinguishMode.value },
      },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    await monitor.loadSnapshot()
    await refreshTimeline()
    toast('灭火任务已创建，等待发布与车端确认')
  } catch (reason) {
    toast(errorMessage(reason))
  } finally {
    working.value = false
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
  if (!robot.value) return
  try {
    const plans = (await api.get('/patrol-plans')).data as Array<{
      id: string
      robot_id: string
      enabled: boolean
    }>
    const plan = plans.find((item) => item.robot_id === robot.value?.id && item.enabled)
    if (!plan) throw new Error('当前车辆没有可执行的巡检计划')
    await api.post(
      '/tasks/patrol-plan',
      { robot_id: robot.value.vehicle_id, patrol_plan_id: plan.id, parameters: { source: 'OPERATIONS_HMI' } },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    await monitor.loadSnapshot()
    toast('巡检任务已创建')
  } catch (reason) {
    toast(errorMessage(reason))
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
    )
      window.clearInterval(stopTimer)
  }, 500)
}
async function stop() {
  if (!robot.value) return
  try {
    stopOperation.value = (
      await api.post(
        `/robots/${robot.value.vehicle_id}/stop-patrol`,
        {},
        { headers: { 'Idempotency-Key': newUuid() } },
      )
    ).data
    if (stopOperation.value) void pollStop(stopOperation.value.id)
    toast('正在独立确认任务取消与车辆静止')
  } catch (reason) {
    toast(errorMessage(reason))
  }
}
async function home() {
  if (!robot.value) return
  const preset = monitor.snapshot.navigation_presets?.find((item) => item.category === 'WAITING_AREA')
  try {
    await api.post(
      `/robots/${robot.value.vehicle_id}/commands/return-dock`,
      { params: { navigation_preset_id: preset?.id } },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    toast('返回等待区命令已创建')
  } catch (reason) {
    toast(errorMessage(reason))
  }
}
async function estop() {
  if (!robot.value) return
  try {
    await api.post(
      `/robots/${robot.value.vehicle_id}/commands/emergency-stop`,
      {},
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    toast('软件急停已发送，等待车端应用层 ACK')
  } catch (reason) {
    toast(errorMessage(reason))
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
    <SituationBanner
      :state="situation"
      :alarm="primaryAlarm"
      @select="primaryAlarmId = primaryAlarm?.id || null"
    />
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
          <small>{{ robot ? `${robot.vehicle_id} · ${robot.online_state}` : 'NO ROBOT' }}</small>
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
        <RiskTelemetryRibbon :robot="robot" :freshness="freshness" :stream="roofStream" />
        <template v-if="primaryAlarm">
          <PrimaryAlarmPanel
            :alarm="primaryAlarm"
            :timeline="timeline"
            :mode="extinguishMode"
            :disabled-reason="extinguishReason"
            :permissions="{
              ack: auth.can('alarm.ack'),
              confirm: auth.can('alarm.confirm'),
              resolve: auth.can('alarm.resolve'),
            }"
            @transition="transition"
            @update:mode="extinguishMode = $event"
            @dispatch="dispatch"
          />
          <section class="panel secondary-alarms">
            <header>
              <strong>其他事件</strong><span>{{ Math.max(0, activeAlarms.length - 1) }}</span>
            </header>
            <button
              v-for="alarm in activeAlarms.filter((item) => item.id !== primaryAlarmId)"
              :key="alarm.id"
              :class="{ active: primaryAlarmId === alarm.id }"
              @click="primaryAlarmId = alarm.id"
            >
              <i :data-level="alarm.severity"></i
              ><span
                >{{ alarm.event_code }}<small>{{ alarm.fire_type }} · {{ alarm.state }}</small></span
              ><time>{{ new Date(alarm.last_seen_at).toLocaleTimeString('zh-CN', { hour12: false }) }}</time>
            </button>
            <div v-if="activeAlarms.length <= 1" class="quiet-state">暂无其他活动事件</div>
          </section>
        </template>
        <section v-else class="panel yd-patrol-card">
          <div class="patrol-state">
            <span class="ptitle">巡检状态</span>
            <strong>{{ patrolStatus }}</strong>
            <span>{{ activeTask ? `任务进行中 · ${patrolCode}` : '设备运行良好' }}</span>
          </div>
          <div class="yd-patrol-meta">
            <div><span>巡检模式</span><br /><b>{{ patrolMode }}</b></div>
            <div><span>任务进度</span><br /><b>{{ patrolProgress }}%</b></div>
          </div>
          <div class="score-ring">{{ patrolProgress }}%</div>
        </section>
      </aside>
    </section>
    <section v-if="stopOperation" class="stop-operation-state" aria-live="polite">
      <strong>正在确认车辆静止</strong>
      <span>任务取消：{{ stopOperation.mission_cancel_state }}</span>
      <span>停止命令：{{ stopOperation.motion_stop_state }}</span>
      <span>连续静止帧 {{ stopOperation.stationary_frames }}/5</span>
      <strong v-if="stopOperation.state === 'VEHICLE_STATIONARY_CONFIRMED'">车辆已停止</strong>
      <strong v-else-if="['PARTIAL_UNCONFIRMED', 'UNCONFIRMED', 'FAILED'].includes(stopOperation.state)"
        >停止结果未完全确认</strong
      >
    </section>
    <section class="yd-command-dock">
      <OperationsCommandDock
        :disabled="working || !autonomousReady"
        :stop-disabled="working || !stopReady"
        :estop-disabled="working || !estopReady"
        :manual-disabled="working || !manualReady || !auth.can('robot.control.manual')"
        :reason="controlReason"
        @patrol="patrol"
        @stop="stop"
        @home="home"
        @estop="estop"
        @manual="manualOpen = true"
      />
    </section>
    <t-drawer v-model:visible="manualOpen" header="手动控制（高级操作）" size="420px" :footer="false">
      <ManualControl :robot="robot" :show-safety="false" @notice="toast" />
      <t-alert
        theme="warning"
        title="安全提示"
        message="手动脉冲 TTL 500 ms；松键、失焦或页面隐藏会发送 stop_motion 并释放租约。真实车辆最终安全仍由车端 watchdog 和物理急停保证。"
      />
    </t-drawer>
  </main>
</template>
