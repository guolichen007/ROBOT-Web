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
const simulated = computed(() => monitor.robot?.simulation?.simulated === true)
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
  // G3-R2 硬件契约：不配置独立红外测温传感器 → 首页固定四项指标，不含温度卡片。
  // 即使后端仍收到历史/兼容 telemetry.thermal 或 top_ir，也不在此展示（热成像视频另由 VideoSurveillancePanel 承载）。
  return [
    {
      key: 'state',
      label: '智能车状态',
      value: state,
      stale: v1 ? freshness.value.statusStale : !freshness.value.online,
    },
    {
      key: 'battery',
      label: '电池',
      value: metric(v1 ? telemetry?.battery?.percentage : robot?.battery, '%'),
      stale: freshness.value.stale,
    },
    {
      key: 'smoke',
      // Canonical 1.3 sensor.smoke 无已验证单位：在标签上常显，避免被误读为 % 或 ppm
      label: v1 ? '烟雾' : '烟雾（单位待验证）',
      // V1 合同字段为 smog.percentage（0–100）；Canonical 1.3 sensor.smoke 单位未定义 → 不加单位
      value: v1 ? metric(telemetry?.smog?.percentage, '%') : metric(robot?.smoke),
      unitNote: v1 ? '' : '单位待验证',
      stale: freshness.value.stale,
    },
    {
      key: 'link',
      label: '通信状态 / 接收延迟',
      value: freshness.value.online ? metric(robot?.agent_state?.receive_latency_ms, 'ms') : '未连接',
      stale: !freshness.value.online,
    },
  ]
})
</script>

<template>
  <p v-if="simulated" class="hmi-sim-banner" role="note" data-testid="simulated-source-banner">
    SIMULATED · 模拟数据源（{{ monitor.robot?.vehicle_id }}）——非真实车辆/传感器
  </p>
  <section class="hmi-kpis" aria-label="车辆实时指标">
    <article
      v-for="card in cards"
      :key="card.key"
      class="panel hmi-kpi"
      :data-stale="card.stale"
      :data-kpi="card.key"
    >
      <span>{{ card.label }}</span>
      <strong :data-unknown="card.value === '--' ? 'true' : 'false'">{{
        card.value === '--' ? '未知' : card.value
      }}</strong>
      <small>{{
        card.value === '--'
          ? '无数据'
          : card.stale
            ? '陈旧 / 离线'
            : simulated
              ? '模拟数据（SIMULATED）'
              : 'unitNote' in card && card.unitNote
                ? `服务器数据 · ${card.unitNote}`
                : '服务器数据'
      }}</small>
    </article>
  </section>
</template>
