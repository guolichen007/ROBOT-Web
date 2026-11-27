<script setup lang="ts">
/**
 * WEB-1 compact console status bar (DESIGN.md §3.1 / §4), WEB-R1B semantics:
 * - 平台 (browser↔platform WebSocket) and 车辆链路 (vehicle↔platform data age) are separate.
 * - 在线 ≠ 就绪: "控制就绪" comes only from the server OperationReadiness.
 * - 无活动任务记录 ≠ 空闲: "空闲" only when the vehicle itself reports IDLE on live data.
 * - 部署环境 (server deployment) and 车辆来源 (vehicle namespace / simulation) are separate badges.
 * - future-dated data is an anomaly, never "live".
 * Anything not positively known from the server is shown as 未知 (never 0, never inferred).
 */
import { computed } from 'vue'
import { useMonitorStore } from '@/stores/monitor'
import { useSystemClock } from '@/composables/useSystemClock'
import type { OperationReadiness } from '@/types'

const props = withDefaults(
  defineProps<{ readiness?: OperationReadiness | null; readinessAuthority?: string }>(),
  { readiness: null, readinessAuthority: 'UNKNOWN' },
)
const monitor = useMonitorStore()
const { now } = useSystemClock()
const robot = computed(() => monitor.robot)

type Level = 'ok' | 'warn' | 'bad' | 'unknown'
interface Dim {
  key: 'platform' | 'link' | 'ready' | 'task' | 'safety'
  label: string
  value: string
  level: Level
}

const SIM_NAMESPACE = /^firebot-sim-[a-z0-9]+(?:-[a-z0-9]+)*$/
const FUTURE_SKEW_S = 2
const vehicleSource = computed<'SIM' | 'REAL' | null>(() => {
  const r = robot.value
  if (!r) return null
  return r.simulation?.simulated === true || SIM_NAMESPACE.test(r.vehicle_id) ? 'SIM' : 'REAL'
})
const deployment = computed(() => {
  const d = robot.value?.deployment_environment
  return d === 'REAL' || d === 'SIM' ? d : null
})
const fixture = computed(() => robot.value?.integration?.source_kind === 'MOCK')

const ageSeconds = computed(() => {
  const at = robot.value?.server_received_at
  if (!at) return null
  return ((now.value?.getTime() ?? Date.now()) - Date.parse(at)) / 1000
})
const dataQuality = computed<'LIVE' | 'STALE' | 'OFFLINE' | 'UNKNOWN' | 'FUTURE'>(() => {
  const age = ageSeconds.value
  if (age === null || Number.isNaN(age)) return 'UNKNOWN'
  if (age < -FUTURE_SKEW_S) return 'FUTURE'
  const stale = robot.value?.integration?.stale_seconds ?? 3
  const offline = robot.value?.integration?.offline_seconds ?? 10
  if (age >= offline) return 'OFFLINE'
  if (age >= stale) return 'STALE'
  return 'LIVE'
})

const activeTask = computed(() =>
  monitor.activeRobotTasks.find((t) => !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(t.status)),
)
const taskLabel: Record<string, string> = {
  PATROL: '巡检',
  RETURN_DOCK: '返航',
  NAVIGATE_TO_PRESET: '目标导航',
  EXTINGUISH: '灭火',
  V1_PATROL: '巡检',
  V1_HOMING: '返航',
  V1_NAVIGATE: '目标导航',
}

const dims = computed<Dim[]>(() => {
  const r = robot.value
  const platform: Dim = monitor.connected
    ? { key: 'platform', label: '平台', value: '已连接', level: 'ok' }
    : { key: 'platform', label: '平台', value: '连接中断', level: 'bad' }
  const q = dataQuality.value
  const link: Dim =
    q === 'FUTURE'
      ? { key: 'link', label: '车辆链路', value: '时间异常（超前）', level: 'bad' }
      : q === 'LIVE'
        ? { key: 'link', label: '车辆链路', value: '实时', level: 'ok' }
        : q === 'STALE'
          ? { key: 'link', label: '车辆链路', value: `陈旧 ${ageSeconds.value?.toFixed(0)}s`, level: 'warn' }
          : q === 'OFFLINE'
            ? { key: 'link', label: '车辆链路', value: '离线', level: 'bad' }
            : { key: 'link', label: '车辆链路', value: '未知', level: 'unknown' }
  const ops = props.readiness
  const ready: Dim =
    props.readinessAuthority !== 'SERVER' || !ops
      ? { key: 'ready', label: '控制就绪', value: '未知', level: 'unknown' }
      : ops.control_frozen
        ? { key: 'ready', label: '控制就绪', value: '已冻结', level: 'warn' }
        : ops.actions.stop_motion?.allowed
          ? { key: 'ready', label: '控制就绪', value: '就绪', level: 'ok' }
          : { key: 'ready', label: '控制就绪', value: '未就绪', level: 'warn' }
  const t = activeTask.value
  const mode = (r?.current_mode ?? r?.mode) as string | undefined
  const task: Dim = t
    ? {
        key: 'task',
        label: '任务',
        value: `${taskLabel[t.type] ?? t.type} · ${Math.round(Number(t.progress) || 0)}%`,
        level: 'ok',
      }
    : q === 'LIVE' && mode === 'IDLE'
      ? { key: 'task', label: '任务', value: '空闲（车端上报）', level: 'ok' }
      : { key: 'task', label: '任务', value: '无任务记录 · 车端状态未知', level: 'unknown' }
  const estopSupport = r?.data_channels?.estop?.support_state
  const safety: Dim =
    r?.estop_active === true
      ? { key: 'safety', label: '安全', value: '软件急停锁存', level: 'bad' }
      : estopSupport === 'CONNECTED' && r?.estop_active === false
        ? { key: 'safety', label: '安全', value: '未急停', level: 'ok' }
        : { key: 'safety', label: '安全', value: '急停状态未知', level: 'unknown' }
  return [platform, link, ready, task, safety]
})

const pose = computed(() => {
  const r = robot.value
  const known =
    dataQuality.value === 'LIVE' || dataQuality.value === 'STALE'
      ? typeof r?.x === 'number' && typeof r?.y === 'number' && typeof r?.theta === 'number'
      : false
  if (!known || !r) return null
  return {
    x: r.x!.toFixed(2),
    y: r.y!.toFixed(2),
    yaw: ((r.theta! * 180) / Math.PI).toFixed(1),
    localization: r.localization_status || '未知',
    at: r.server_received_at
      ? new Date(r.server_received_at).toLocaleTimeString('zh-CN', { hour12: false })
      : '',
    stale: dataQuality.value === 'STALE',
  }
})

function select(event: Event): void {
  monitor.selectRobot((event.target as HTMLSelectElement).value)
}
</script>

<template>
  <section class="console-status" aria-label="车辆与系统状态" data-testid="console-status">
    <label class="console-vehicle">
      <span>车辆</span>
      <select
        :value="robot?.vehicle_id ?? ''"
        data-testid="console-vehicle"
        :disabled="monitor.enabledRobots.length < 2"
        @change="select"
      >
        <option v-if="!robot" value="">未选择</option>
        <option v-for="item in monitor.enabledRobots" :key="item.id" :value="item.vehicle_id">
          {{ item.vehicle_id }}
        </option>
      </select>
    </label>
    <span
      class="console-env"
      :data-env="deployment ?? 'UNKNOWN'"
      data-testid="console-deploy"
      title="平台部署环境（服务端 FIREBOT_DEPLOYMENT_ENV）"
      >部署 {{ deployment ?? '未知' }}</span
    >
    <span
      class="console-env"
      :data-env="vehicleSource ?? 'UNKNOWN'"
      data-testid="console-env"
      :title="
        vehicleSource === 'SIM'
          ? '模拟车辆：数据与控制均与真实车辆隔离'
          : vehicleSource === 'REAL'
            ? '真实车辆身份'
            : '车辆来源未知'
      "
      >{{ vehicleSource ? `车辆 ${vehicleSource}` : '车辆来源未知' }}{{ fixture ? ' · 演示数据' : '' }}</span
    >
    <ul class="console-dims">
      <li v-for="dim in dims" :key="dim.key" :data-dim="dim.key" :data-level="dim.level">
        <span>{{ dim.label }}</span
        ><b>{{ dim.value }}</b>
      </li>
    </ul>
    <p class="console-pose" data-testid="console-pose" :data-stale="pose?.stale ? 'true' : 'false'">
      <template v-if="pose">
        <span
          >X <b>{{ pose.x }}</b></span
        ><span
          >Y <b>{{ pose.y }}</b></span
        ><span
          >yaw <b>{{ pose.yaw }}°</b></span
        ><span
          >定位 <b>{{ pose.localization }}</b></span
        ><time>{{ pose.stale ? '旧数据 ' : '' }}{{ pose.at }}</time>
      </template>
      <template v-else
        ><span>位姿 <b>未知</b></span></template
      >
    </p>
  </section>
</template>
