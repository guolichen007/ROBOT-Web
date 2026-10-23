<script setup lang="ts">
import { computed } from 'vue'
import { useMonitorStore } from '@/stores/monitor'
import { useSystemClock } from '@/composables/useSystemClock'
import { agentFreshness, metric } from '@/lib/vehicle-agent'

const monitor = useMonitorStore()
const { now } = useSystemClock()
const freshness = computed(() =>
  agentFreshness(monitor.robot, monitor.connected, now.value?.getTime() ?? Date.now()),
)
const cards = computed(() => {
  const robot = monitor.robot
  const v1 = robot?.agent_protocol === 'VEHICLE_AGENT_V1'
  const telemetry = robot?.agent_state?.telemetry
  const chassis = robot?.agent_state?.status?.programs?.chassis
  const state = !freshness.value.online
    ? '离线'
    : v1
      ? robot?.agent_state?.status?.online !== true
        ? '--'
        : chassis === true || chassis === 'running' || chassis === 'ready'
          ? '已启动'
          : chassis === false || chassis === 'stopped'
            ? '未启动'
            : '--'
      : '在线'
  return [
    { label: '智能车状态', value: state, stale: v1 ? freshness.value.statusStale : !freshness.value.online },
    {
      label: '电池',
      value: metric(v1 ? telemetry?.battery?.percentage : robot?.battery, '%'),
      stale: freshness.value.stale,
    },
    {
      label: '温度',
      value: metric(v1 ? telemetry?.thermal?.max_temp : robot?.top_ir, '℃'),
      stale: freshness.value.stale,
    },
    {
      label: '烟雾',
      value: metric(v1 ? telemetry?.smog?.percentage : robot?.smoke, '%'),
      stale: freshness.value.stale,
    },
    {
      label: '通信状态 / 接收延迟',
      value: freshness.value.online ? metric(robot?.agent_state?.receive_latency_ms, 'ms') : '未连接',
      stale: !freshness.value.online,
    },
  ]
})
</script>

<template>
  <section class="hmi-kpis" aria-label="车辆实时指标">
    <article v-for="card in cards" :key="card.label" class="panel hmi-kpi" :data-stale="card.stale">
      <span>{{ card.label }}</span>
      <strong>{{ card.value }}</strong>
      <small>{{ card.stale ? '陈旧 / 离线' : '服务器数据' }}</small>
    </article>
  </section>
</template>
