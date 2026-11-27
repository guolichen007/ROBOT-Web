<script setup lang="ts">
/**
 * WEB-1 compact console status bar (DESIGN.md §3.1 / §4).
 * Four independent dimensions — 通信 / 设备 / 任务 / 安全 — never merged into one colour.
 * Everything not positively known from the server is shown as 未知 (never 0, never inferred).
 */
import { computed } from 'vue'
import { useMonitorStore } from '@/stores/monitor'
import { useSystemClock } from '@/composables/useSystemClock'

const monitor = useMonitorStore()
const { now } = useSystemClock()
const robot = computed(() => monitor.robot)

type Level = 'ok' | 'warn' | 'bad' | 'unknown'
interface Dim {
  key: 'link' | 'device' | 'task' | 'safety'
  label: string
  value: string
  level: Level
}

const SIM_NAMESPACE = /^firebot-sim-[a-z0-9]+(?:-[a-z0-9]+)*$/
const environment = computed<'SIM' | 'REAL' | null>(() => {
  const r = robot.value
  if (!r) return null
  return r.simulation?.simulated === true || SIM_NAMESPACE.test(r.vehicle_id) ? 'SIM' : 'REAL'
})

const ageSeconds = computed(() => {
  const at = robot.value?.server_received_at
  if (!at) return null
  return Math.max(0, ((now.value?.getTime() ?? Date.now()) - Date.parse(at)) / 1000)
})
const dataQuality = computed<'LIVE' | 'STALE' | 'OFFLINE' | 'UNKNOWN'>(() => {
  const age = ageSeconds.value
  if (age === null) return 'UNKNOWN'
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
  const link: Dim = !monitor.connected
    ? { key: 'link', label: '通信', value: '平台连接中断', level: 'bad' }
    : dataQuality.value === 'LIVE'
      ? { key: 'link', label: '通信', value: '实时', level: 'ok' }
      : dataQuality.value === 'STALE'
        ? { key: 'link', label: '通信', value: `陈旧 ${ageSeconds.value?.toFixed(0)}s`, level: 'warn' }
        : dataQuality.value === 'OFFLINE'
          ? { key: 'link', label: '通信', value: '车辆离线', level: 'bad' }
          : { key: 'link', label: '通信', value: '未知', level: 'unknown' }
  const online = r?.online_state
  const device: Dim =
    online === 'ONLINE'
      ? { key: 'device', label: '设备', value: '在线', level: 'ok' }
      : online === 'STALE'
        ? { key: 'device', label: '设备', value: '陈旧', level: 'warn' }
        : online === 'OFFLINE'
          ? { key: 'device', label: '设备', value: '离线', level: 'bad' }
          : { key: 'device', label: '设备', value: '未知', level: 'unknown' }
  const t = activeTask.value
  const task: Dim = t
    ? {
        key: 'task',
        label: '任务',
        value: `${taskLabel[t.type] ?? t.type} · ${Math.round(Number(t.progress) || 0)}%`,
        level: 'ok',
      }
    : r
      ? { key: 'task', label: '任务', value: '空闲', level: 'ok' }
      : { key: 'task', label: '任务', value: '未知', level: 'unknown' }
  const estopSupport = r?.data_channels?.estop?.support_state
  // 安全方向：车端报告“急停已锁存”永远直接显示，绝不因通道未确认而降级为“未知”；
  // 只有“未急停”在急停通道未 CONNECTED 时才降级为“未知”（不能证明未急停）。
  const safety: Dim =
    r?.estop_active === true
      ? { key: 'safety', label: '安全', value: '软件急停锁存', level: 'bad' }
      : estopSupport === 'CONNECTED' && r?.estop_active === false
        ? { key: 'safety', label: '安全', value: '未急停', level: 'ok' }
        : { key: 'safety', label: '安全', value: '急停状态未知', level: 'unknown' }
  return [link, device, task, safety]
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
      :data-env="environment ?? 'UNKNOWN'"
      data-testid="console-env"
      :title="
        environment === 'SIM'
          ? '模拟环境：数据与控制均与真实车辆隔离'
          : environment === 'REAL'
            ? '真实车辆'
            : '环境未知'
      "
      >{{ environment ?? '环境未知' }}</span
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
