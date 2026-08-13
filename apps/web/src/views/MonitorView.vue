<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  AlarmIcon,
  ControlPlatformIcon,
  HomeIcon,
  PoweroffIcon,
  StopCircleIcon,
} from 'tdesign-icons-vue-next'
import ManualControl from '@/components/ManualControl.vue'
import MapCanvas from '@/components/MapCanvas.vue'
import StateChip from '@/components/StateChip.vue'
import VideoCard from '@/components/VideoCard.vue'
import { api, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import { compareOperationalAlarms, operationalSituation } from '@/lib/operations'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'
import type { Alarm, DataSupportState, DetectionCoverage, ParkingSlot, StopOperation } from '@/types'

const auth = useAuthStore()
const monitor = useMonitorStore()
const selectedSlot = ref<ParkingSlot | null>(null)
const selectedAlarm = ref<Alarm | null>(null)
const extinguishMode = ref('DEPLOY_THEN_SPRAY')
const notice = ref<{ message: string; tone: string } | null>(null)
const working = ref(false)
const manualOpen = ref(false)
const videoTab = ref('roof_rgb')
const coverage = ref<DetectionCoverage | null>(null)
const stopOperation = ref<StopOperation | null>(null)
let coverageTimer = 0
let stopTimer = 0
const STOP_OPERATION_STORAGE_KEY = 'firebot.stop-operation-id'

const trajectory = computed(() => monitor.snapshot.trajectories[0]?.path_json || [])
const streams = computed(() =>
  Object.fromEntries(monitor.snapshot.streams.map((item) => [item.camera_type, item])),
)
const alarms = computed(() => [...monitor.snapshot.alarms].sort(compareOperationalAlarms).slice(0, 6))
const robot = computed(() => monitor.robot)
const activeAlarm = computed(() => alarms.value[0])
const autonomousReady = computed(() => Boolean(robot.value?.autonomous_task_ready?.patrol))
const stopReady = computed(() => Boolean(robot.value?.safety_command_ready?.stop_motion))
const estopReady = computed(() => Boolean(robot.value?.safety_command_ready?.emergency_stop))
const manualReady = computed(() => Boolean(robot.value?.manual_control_ready))
const controlEnabled = autonomousReady
const controlReason = computed(
  () =>
    robot.value?.readiness_reasons?.join('、') ||
    robot.value?.control_disabled_reason ||
    (robot.value?.online_state !== 'ONLINE' ? '车辆不在线' : '控制合同尚未验证'),
)
const situationState = computed(() => {
  return operationalSituation({
    criticalFire: activeAlarm.value?.severity === 'CRITICAL',
    websocketConnected: monitor.connected,
    onlineState: robot.value?.online_state,
    localizationStatus: robot.value?.localization_status,
    estopActive: robot.value?.estop_active,
    estopSupport: channelState('estop'),
  })
})
const stopStateText = computed(
  () =>
    ({
      STOP_REQUESTED: '正在停止巡检',
      TASK_CANCEL_ACCEPTED: '巡检任务已取消，等待停止确认',
      STOP_COMMAND_ACCEPTED: '停止命令已接受',
      VERIFYING_STATIONARY: '正在用新鲜遥测确认车辆静止',
      STATIONARY_CONFIRMED_CANCEL_PENDING: '车辆已静止，任务取消未确认',
      VEHICLE_STATIONARY_CONFIRMED: '车辆已停止',
      UNCONFIRMED: '停止未确认',
    })[stopOperation.value?.state || ''] || '',
)

const extinguishModes = [
  { value: 'DEPLOY_BLANKET', title: '展开灭火帐', description: '展开并覆盖目标车辆' },
  { value: 'SPRAY_AGENT', title: '喷射灭火剂', description: '对准目标执行药剂喷射' },
  { value: 'DEPLOY_THEN_SPRAY', title: '帐幕后喷射', description: '先展开灭火帐，再喷射灭火剂' },
]

function toast(message: string, tone = ''): void {
  notice.value = { message, tone }
  window.setTimeout(() => {
    if (notice.value?.message === message) notice.value = null
  }, 4200)
}

function channelState(name: string): DataSupportState {
  return robot.value?.data_channels?.[name]?.support_state || 'NOT_CONNECTED'
}

function closeAlarmDrawer(visible: boolean): void {
  if (!visible) selectedAlarm.value = null
}

function valueOrState(value: number | null | undefined, channel: string, suffix = ''): string {
  if (channelState(channel) !== 'CONNECTED' || value == null) {
    return channelState(channel) === 'UNSUPPORTED' ? '当前车型不支持' : '未接入'
  }
  return `${value.toFixed(channel === 'smoke' ? 3 : 1)}${suffix}`
}

async function loadCoverage(): Promise<void> {
  if (!robot.value) return
  try {
    coverage.value = (await api.get(`/robots/${robot.value.vehicle_id}/detection-coverage`)).data
  } catch {
    coverage.value = null
  }
}

async function createManualAlarm(): Promise<void> {
  if (!selectedSlot.value || !monitor.snapshot.map_version) return
  working.value = true
  try {
    const { data } = await api.post(
      '/alarms/manual',
      {
        parking_slot_id: selectedSlot.value.id,
        fire_type: 'unknown',
        note: `监控地图人工上报：${selectedSlot.value.code}`,
        map_version: monitor.snapshot.map_version.version,
        severity: 'HIGH',
        media: {},
      },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    selectedAlarm.value = data
    await monitor.loadSnapshot()
    toast(`${selectedSlot.value.code} 人工火情已创建`, 'danger')
  } catch (reason) {
    toast(errorMessage(reason), 'danger')
  } finally {
    working.value = false
  }
}

async function transition(alarm: Alarm, action: 'acknowledge' | 'confirm' | 'resolve'): Promise<void> {
  try {
    selectedAlarm.value = (await api.post(`/alarms/${alarm.id}/${action}`)).data
    await monitor.loadSnapshot()
    toast('火情状态已更新')
  } catch (reason) {
    toast(errorMessage(reason), 'danger')
  }
}

async function dispatch(alarm: Alarm): Promise<void> {
  if (!robot.value) return
  working.value = true
  try {
    await api.post(
      `/alarms/${alarm.id}/create-task`,
      {
        robot_id: robot.value.vehicle_id,
        trajectory_id: monitor.snapshot.trajectories[0]?.id,
        parameters: { source: 'MONITOR', extinguish_mode: extinguishMode.value },
      },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    await monitor.loadSnapshot()
    selectedAlarm.value = null
    toast('灭火任务已创建，等待车端 ACK', 'ok')
  } catch (reason) {
    toast(errorMessage(reason), 'danger')
  } finally {
    working.value = false
  }
}

async function startPatrol(): Promise<void> {
  if (!robot.value) return
  working.value = true
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
      {
        robot_id: robot.value.vehicle_id,
        patrol_plan_id: plan.id,
        parameters: { source: 'OPERATIONS_HMI', patrol_scope: 'FULL_ROUTE' },
      },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    await monitor.loadSnapshot()
    toast('巡检任务已创建，等待派发与车辆接受', 'ok')
  } catch (reason) {
    toast(errorMessage(reason), 'danger')
  } finally {
    working.value = false
  }
}

async function pollStop(id: string): Promise<void> {
  window.clearInterval(stopTimer)
  stopTimer = window.setInterval(async () => {
    try {
      stopOperation.value = (await api.get(`/stop-operations/${id}`)).data
      if (
        ['VEHICLE_STATIONARY_CONFIRMED', 'FAILED', 'UNCONFIRMED'].includes(stopOperation.value?.state || '')
      ) {
        window.clearInterval(stopTimer)
        sessionStorage.removeItem(STOP_OPERATION_STORAGE_KEY)
      }
    } catch {
      window.clearInterval(stopTimer)
    }
  }, 500)
}

async function stopPatrol(): Promise<void> {
  if (!robot.value) return
  working.value = true
  try {
    stopOperation.value = (
      await api.post(
        `/robots/${robot.value.vehicle_id}/stop-patrol`,
        {},
        { headers: { 'Idempotency-Key': newUuid() } },
      )
    ).data
    if (stopOperation.value) {
      sessionStorage.setItem(STOP_OPERATION_STORAGE_KEY, stopOperation.value.id)
      void pollStop(stopOperation.value.id)
    }
    toast('已同时请求取消巡检和停止运动；正在确认车辆静止', 'warn')
  } catch (reason) {
    toast(errorMessage(reason), 'danger')
  } finally {
    working.value = false
  }
}

async function returnWaiting(): Promise<void> {
  if (!robot.value) return
  const preset = monitor.snapshot.navigation_presets?.find((item) => item.category === 'WAITING_AREA')
  try {
    await api.post(
      `/robots/${robot.value.vehicle_id}/commands/return-dock`,
      {
        params: {
          destination_kind: 'WAITING_AREA',
          navigation_preset_id: preset?.id,
          pose: preset?.pose_json,
        },
      },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    toast('返回等待区已创建，等待车端 ACK', 'ok')
  } catch (reason) {
    toast(errorMessage(reason), 'danger')
  }
}

async function emergencyStop(): Promise<void> {
  if (!robot.value) return
  working.value = true
  try {
    const { data } = await api.post(
      `/robots/${robot.value.vehicle_id}/commands/emergency-stop`,
      {},
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    toast(
      data.lifecycle_status === 'PUBLISHED_UNCONFIRMED'
        ? '软件急停未送达/未确认'
        : '软件急停已发送，等待车端应用层 ACK',
      'danger',
    )
  } catch (reason) {
    toast(errorMessage(reason), 'danger')
  } finally {
    working.value = false
  }
}

onMounted(() => {
  if (!monitor.snapshot.map_version) void monitor.start()
  const pendingStop = sessionStorage.getItem(STOP_OPERATION_STORAGE_KEY)
  if (pendingStop) {
    void api
      .get(`/stop-operations/${pendingStop}`)
      .then(({ data }) => {
        stopOperation.value = data
        if (!['VEHICLE_STATIONARY_CONFIRMED', 'FAILED', 'UNCONFIRMED'].includes(data.state))
          void pollStop(pendingStop)
        else sessionStorage.removeItem(STOP_OPERATION_STORAGE_KEY)
      })
      .catch(() => sessionStorage.removeItem(STOP_OPERATION_STORAGE_KEY))
  }
  void loadCoverage()
  coverageTimer = window.setInterval(() => void loadCoverage(), 1000)
})
onUnmounted(() => {
  window.clearInterval(coverageTimer)
  window.clearInterval(stopTimer)
})
</script>

<template>
  <div class="monitor-page industrial-hmi">
    <div v-if="notice" class="toast" :class="notice.tone">{{ notice.message }}</div>
    <section v-if="activeAlarm" class="fire-banner" @click="selectedAlarm = activeAlarm">
      <AlarmIcon /><strong>{{ activeAlarm.state === 'NEW' ? '主动发现火情' : '活动火情' }}</strong>
      <span>{{ activeAlarm.event_code }}</span
      ><span>{{ activeAlarm.fire_type }}</span
      ><span>{{ activeAlarm.severity }}</span> <StateChip :value="activeAlarm.state" /><button>
        查看并处置
      </button>
    </section>
    <section v-else class="normal-banner" :data-state="situationState">
      <span>{{
        situationState === 'NORMAL'
          ? '运行态势正常'
          : situationState === 'OFFLINE_UNKNOWN'
            ? '车辆离线或实时链路中断，无法确认现场态势'
            : '系统降级，现场态势待确认'
      }}</span>
      <small>{{ situationState }}</small>
    </section>

    <div class="operations-grid">
      <section class="panel map-panel">
        <div class="panel-heading">
          <div>
            <h2>二维停车场地图</h2>
            <p>车位、规划路线、车辆位置与右侧检测覆盖</p>
          </div>
          <div class="map-state">
            <span>{{ robot?.x?.toFixed?.(2) ?? '--' }}, {{ robot?.y?.toFixed?.(2) ?? '--' }} m</span
            ><span
              >航向 {{ robot?.theta == null ? '--' : `${((robot.theta * 180) / Math.PI).toFixed(1)}°` }}</span
            >
          </div>
        </div>
        <MapCanvas
          :map-version="monitor.snapshot.map_version"
          :slots="monitor.snapshot.parking_slots"
          :inspection-points="monitor.snapshot.inspection_points"
          :extinguish-points="monitor.snapshot.extinguish_points"
          :trajectory="trajectory"
          :robot="robot"
          :alarms="monitor.snapshot.alarms"
          :coverage="coverage"
          :selected-slot-id="selectedSlot?.id"
          @slot-click="selectedSlot = $event"
        />
        <div v-if="selectedSlot" class="map-selection">
          <strong>{{ selectedSlot.code }}</strong
          ><span>{{
            coverage?.covered_parking_slot_ids.includes(selectedSlot.id)
              ? '当前位于右侧检测覆盖内'
              : '当前不在检测覆盖内'
          }}</span
          ><t-button
            v-if="auth.can('alarm.confirm')"
            theme="danger"
            variant="outline"
            :loading="working"
            @click="createManualAlarm"
            >人工上报火情</t-button
          ><button @click="selectedSlot = null">取消选择</button>
        </div>
      </section>

      <aside class="situation-column">
        <section class="panel primary-video">
          <t-tabs v-model="videoTab" size="large">
            <t-tab-panel value="roof_rgb" label="车顶实时相机"
              ><VideoCard :stream="streams.roof_rgb" title="车顶 RGB" prominent
            /></t-tab-panel>
            <t-tab-panel value="roof_thermal" label="顶部热像"
              ><VideoCard :stream="streams.roof_thermal" title="顶部热像" prominent
            /></t-tab-panel>
            <t-tab-panel value="bottom_ir" label="车底红外"
              ><VideoCard :stream="streams.bottom_ir" title="车底红外" prominent
            /></t-tab-panel>
          </t-tabs>
        </section>
        <section class="sensor-strip panel">
          <article>
            <span>烟雾浓度</span><strong>{{ valueOrState(robot?.smoke, 'smoke') }}</strong
            ><StateChip :value="channelState('smoke')" />
          </article>
          <article>
            <span>顶部红外</span><strong>{{ valueOrState(robot?.top_ir, 'top_ir', '°C') }}</strong
            ><StateChip :value="channelState('top_ir')" />
          </article>
          <article>
            <span>车底红外</span><strong>{{ valueOrState(robot?.bottom_ir, 'bottom_ir', '°C') }}</strong
            ><StateChip :value="channelState('bottom_ir')" />
          </article>
        </section>
        <section class="panel active-alarm-list">
          <div class="panel-heading">
            <h3>活动报警</h3>
            <RouterLink to="/alarms">全部记录</RouterLink>
          </div>
          <div v-if="!alarms.length" class="quiet-state">当前无活动火情</div>
          <button v-for="alarm in alarms" :key="alarm.id" @click="selectedAlarm = alarm">
            <i :data-level="alarm.severity"></i
            ><span
              ><strong>{{ alarm.event_code }}</strong
              ><small
                >{{ alarm.fire_type }} · {{ new Date(alarm.last_seen_at).toLocaleTimeString() }}</small
              ></span
            ><StateChip :value="alarm.state" />
          </button>
        </section>
      </aside>
    </div>

    <div
      v-if="stopStateText"
      class="stop-progress"
      :class="stopOperation?.state === 'VEHICLE_STATIONARY_CONFIRMED' ? 'complete' : ''"
    >
      <StopCircleIcon /><strong>{{ stopStateText }}</strong
      ><span
        v-if="
          stopOperation?.state === 'VERIFYING_STATIONARY' ||
          stopOperation?.state === 'VEHICLE_STATIONARY_CONFIRMED'
        "
        >连续静止帧 {{ stopOperation.stationary_frames }}/5</span
      >
    </div>
    <section class="command-dock panel">
      <div class="command-context">
        <strong>{{ monitor.activeTask?.type === 'PATROL' ? '巡检中' : robot?.mode || '空闲' }}</strong
        ><span>{{ monitor.activeTask?.phase || '等待操作指令' }}</span
        ><small v-if="!controlEnabled">控制不可用：{{ controlReason }}</small>
      </div>
      <t-tooltip :content="controlReason" :disabled="controlEnabled"
        ><span
          ><t-button
            size="large"
            theme="success"
            :disabled="working || !controlEnabled || !auth.can('patrol.create')"
            @click="startPatrol"
            ><template #icon><ControlPlatformIcon /></template>开始巡检</t-button
          ></span
        ></t-tooltip
      >
      <t-button
        size="large"
        theme="warning"
        :disabled="working || !stopReady || !auth.can('robot.control.stop')"
        @click="stopPatrol"
        ><template #icon><StopCircleIcon /></template>停止巡检</t-button
      >
      <t-tooltip :content="controlReason" :disabled="controlEnabled"
        ><span
          ><t-button
            size="large"
            :disabled="working || !controlEnabled || !auth.can('robot.control.task')"
            @click="returnWaiting"
            ><template #icon><HomeIcon /></template>返回等待区</t-button
          ></span
        ></t-tooltip
      >
      <t-button
        size="large"
        theme="danger"
        :disabled="working || !estopReady || !auth.can('robot.control.estop')"
        @click="emergencyStop"
        ><template #icon><PoweroffIcon /></template>软件紧急停止</t-button
      >
      <t-button
        variant="outline"
        :disabled="!manualReady || !auth.can('robot.control.manual')"
        @click="manualOpen = true"
        >手动控制</t-button
      >
      <small v-if="robot?.integration?.source_kind === 'ROS_COMPAT' && !estopReady" class="safety-unavailable"
        >车辆未提供软件急停接口</small
      >
    </section>

    <t-drawer v-model:visible="manualOpen" header="手动控制（高级操作）" size="420px" :footer="false"
      ><ManualControl :robot="robot" :show-safety="false" @notice="toast" /><t-alert
        theme="warning"
        title="安全提示"
        message="手动脉冲 TTL 500 ms；松键、失焦或页面隐藏会发送 stop_motion 并释放租约。真实车辆最终安全仍由车端 watchdog 和物理急停保证。"
    /></t-drawer>

    <t-drawer
      :visible="Boolean(selectedAlarm)"
      header="火情确认与处置"
      size="520px"
      :footer="false"
      @update:visible="closeAlarmDrawer"
    >
      <div v-if="selectedAlarm" class="fire-drawer">
        <div class="fire-summary">
          <AlarmIcon />
          <div>
            <strong>{{ selectedAlarm.event_code }}</strong
            ><span>{{ selectedAlarm.fire_type }} · {{ selectedAlarm.severity }}</span>
          </div>
          <StateChip :value="selectedAlarm.state" />
        </div>
        <dl>
          <div>
            <dt>发现方式</dt>
            <dd>{{ selectedAlarm.detection_method }}</dd>
          </div>
          <div>
            <dt>重复次数</dt>
            <dd>{{ selectedAlarm.occurrence_count }}</dd>
          </div>
        </dl>
        <div class="fire-actions">
          <t-button
            v-if="selectedAlarm.state === 'NEW' && auth.can('alarm.ack')"
            @click="transition(selectedAlarm, 'acknowledge')"
            >确认收到</t-button
          ><t-button
            v-if="['NEW', 'ACKNOWLEDGED'].includes(selectedAlarm.state) && auth.can('alarm.confirm')"
            theme="warning"
            @click="transition(selectedAlarm, 'confirm')"
            >确认火情</t-button
          >
        </div>
        <section v-if="selectedAlarm.state === 'CONFIRMED'" class="extinguish-choice">
          <h3>选择灭火处理方式</h3>
          <label
            v-for="mode in extinguishModes"
            :key="mode.value"
            :class="{ selected: extinguishMode === mode.value }"
            ><input v-model="extinguishMode" type="radio" :value="mode.value" /><strong>{{
              mode.title
            }}</strong
            ><small>{{ mode.description }}</small></label
          ><t-button
            block
            theme="danger"
            size="large"
            :loading="working"
            :disabled="!controlEnabled"
            @click="dispatch(selectedAlarm)"
            >创建灭火任务</t-button
          >
        </section>
        <t-button
          v-if="auth.can('alarm.resolve')"
          variant="outline"
          @click="transition(selectedAlarm, 'resolve')"
          >标记已解决</t-button
        >
      </div>
    </t-drawer>
  </div>
</template>
